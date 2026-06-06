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


# In-memory storage mock
_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_download_object(key: str) -> bytes:
    if key not in _mock_storage:
        raise RuntimeError(f"Object not found: {key}")
    return _mock_storage[key]


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
    "name": "Basic Template",
    "template_type": "device",
    "vendor": "V",
    "model": "M",
    "sections": [
        {
            "name": "Config",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
            ],
        }
    ],
}


def make_zip_file(content: bytes = b"PK\x03\x04test driver content"):
    return ("driver.zip", content, "application/zip")


async def create_driver_package(
    client, name="Test Driver", description=None, connection_type="Management"
):
    filename, content, _ = make_zip_file()
    data = {"name": name, "connection_type": connection_type}
    if description:
        data["description"] = description
    resp = await client.post(
        "/drivers",
        data=data,
        files={"file": (filename, io.BytesIO(content), "application/zip")},
    )
    return resp


# --- CRUD tests ---


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_driver(mock_up, client):
    resp = await create_driver_package(client, "My Driver", "A test driver")
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Driver"
    assert data["description"] == "A test driver"
    assert data["connection_type"] == "Management"
    assert data["filename"] == "driver.zip"
    assert data["uploaded_by"] == "testadmin"
    assert len(data["sha256"]) == 64


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_driver_duplicate_name(mock_up, client):
    resp1 = await create_driver_package(client, "Duplicate Driver")
    assert resp1.status_code == 201
    resp2 = await create_driver_package(client, "Duplicate Driver")
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_create_driver_invalid_extension(client):
    resp = await client.post(
        "/drivers",
        data={"name": "Bad Driver", "connection_type": "Management"},
        files={"file": ("driver.exe", io.BytesIO(b"bad"), "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert "file type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_driver_exceeds_size_limit(mock_up, client):
    big_content = b"x" * (10_485_760 + 1)
    resp = await client.post(
        "/drivers",
        data={"name": "Big Driver", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(big_content), "application/zip")},
    )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_list_drivers(mock_up, client):
    await create_driver_package(client, "Alpha Driver")
    await create_driver_package(client, "Beta Driver")
    resp = await client.get("/drivers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["name"] == "Alpha Driver"
    assert data["items"][1]["name"] == "Beta Driver"


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_get_driver(mock_up, client):
    create_resp = await create_driver_package(client, "Get Test")
    driver_id = create_resp.json()["id"]
    resp = await client.get(f"/drivers/{driver_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Get Test"


@pytest.mark.asyncio
async def test_get_driver_not_found(client):
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/drivers/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_update_driver_metadata(mock_up, client):
    create_resp = await create_driver_package(client, "Old Name")
    driver_id = create_resp.json()["id"]
    resp = await client.put(
        f"/drivers/{driver_id}",
        json={"name": "New Name", "description": "Updated desc"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["description"] == "Updated desc"


@pytest.mark.asyncio
async def test_update_driver_not_found(client):
    """PUT on a nonexistent driver returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.put(
        f"/drivers/{fake_id}",
        json={"name": "Ghost Driver"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_update_driver_duplicate_name(mock_up, client):
    await create_driver_package(client, "Existing Name")
    create_resp = await create_driver_package(client, "Other Name")
    driver_id = create_resp.json()["id"]
    resp = await client.put(
        f"/drivers/{driver_id}",
        json={"name": "Existing Name"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
@patch("app.services.driver_service.download_object", side_effect=mock_download_object)
async def test_download_driver(mock_dl, mock_up, client):
    create_resp = await create_driver_package(client, "Download Test")
    driver_id = create_resp.json()["id"]
    resp = await client.get(f"/drivers/{driver_id}/download")
    assert resp.status_code == 200
    assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_download_driver_not_found(client):
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/drivers/{fake_id}/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
@patch("app.services.driver_service.delete_object", side_effect=mock_delete_object)
async def test_delete_driver(mock_del, mock_up, client):
    create_resp = await create_driver_package(client, "Delete Test")
    driver_id = create_resp.json()["id"]
    resp = await client.delete(f"/drivers/{driver_id}")
    assert resp.status_code == 204
    # Verify it's gone
    resp2 = await client.get(f"/drivers/{driver_id}")
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_driver_not_found(client):
    fake_id = str(uuid.uuid4())
    resp = await client.delete(f"/drivers/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
@patch("app.services.driver_service.delete_object", side_effect=mock_delete_object)
async def test_delete_driver_referenced_by_template(mock_del, mock_up, client):
    create_resp = await create_driver_package(client, "Referenced Driver")
    driver_id = create_resp.json()["id"]
    # Create a template that references this driver
    tmpl_payload = {**TEMPLATE_PAYLOAD, "name": "Driver Template", "driver_id": driver_id}
    tmpl_resp = await client.post("/templates", json=tmpl_payload)
    assert tmpl_resp.status_code == 201
    # Try to delete
    resp = await client.delete(f"/drivers/{driver_id}")
    assert resp.status_code == 409
    assert "templates" in resp.json()["detail"].lower()


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
@patch("app.services.driver_service.delete_object", side_effect=mock_delete_object)
async def test_replace_driver_file(mock_del, mock_up, client):
    create_resp = await create_driver_package(client, "Replace Test")
    driver_id = create_resp.json()["id"]
    new_content = b"new file content"
    resp = await client.put(
        f"/drivers/{driver_id}/file",
        files={"file": ("driver_v2.zip", io.BytesIO(new_content), "application/zip")},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "driver_v2.zip"
    assert resp.json()["size_bytes"] == len(new_content)


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_driver_tar_gz(mock_up, client):
    content = b"fake tar.gz content"
    resp = await client.post(
        "/drivers",
        data={"name": "Tar Driver", "connection_type": "Layer 1 Switch"},
        files={"file": ("driver.tar.gz", io.BytesIO(content), "application/gzip")},
    )
    assert resp.status_code == 201
    assert resp.json()["filename"] == "driver.tar.gz"


# --- Permission tests ---


@pytest.mark.asyncio
async def test_user_cannot_create_driver(user_client):
    filename, content, _ = make_zip_file()
    resp = await user_client.post(
        "/drivers",
        data={"name": "Forbidden", "connection_type": "Management"},
        files={"file": (filename, io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete_driver(client, user_client):
    # Use admin to check the endpoint works, then test user
    resp = await user_client.delete(f"/drivers/{uuid.uuid4()}")
    assert resp.status_code == 403


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_user_can_list_drivers(mock_up, client):
    await create_driver_package(client, "Visible Driver")
    # Switch to user auth
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.get("/drivers")
    assert resp.status_code == 200


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_user_can_get_driver(mock_up, client):
    create_resp = await create_driver_package(client, "Get Visible")
    driver_id = create_resp.json()["id"]
    # Switch to user auth
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.get(f"/drivers/{driver_id}")
    assert resp.status_code == 200


# --- Integration: template + device with driver ---


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_template_with_driver_id(mock_up, client):
    create_resp = await create_driver_package(client, "Template Driver")
    driver_id = create_resp.json()["id"]
    tmpl_payload = {**TEMPLATE_PAYLOAD, "name": "With Driver", "driver_id": driver_id}
    resp = await client.post("/templates", json=tmpl_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["driver_id"] == driver_id
    assert data["driver_name"] == "Template Driver"


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_device_response_includes_driver_info(mock_up, client):
    create_resp = await create_driver_package(client, "Device Driver")
    driver_id = create_resp.json()["id"]
    tmpl_payload = {**TEMPLATE_PAYLOAD, "name": "Driver Template", "driver_id": driver_id}
    tmpl_resp = await client.post("/templates", json=tmpl_payload)
    template_id = tmpl_resp.json()["id"]
    dev_resp = await client.post(
        "/devices",
        json={
            "name": "Test Device",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "TestModel"},
        },
    )
    assert dev_resp.status_code == 201
    data = dev_resp.json()
    assert data["driver_id"] == driver_id
    assert data["driver_name"] == "Device Driver"


@pytest.mark.asyncio
async def test_device_template_requires_driver(client):
    resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 422
    assert "driver" in resp.json()["detail"][0]["msg"].lower()


# --- Connection type tests ---


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_driver_each_connection_type(mock_up, client):
    for i, ct in enumerate(["Management", "Layer 1 Switch", "Layer 2 Switch", "Layer 3 Switch"]):
        resp = await create_driver_package(client, f"Driver {i}", connection_type=ct)
        assert resp.status_code == 201
        assert resp.json()["connection_type"] == ct


@pytest.mark.asyncio
async def test_create_driver_invalid_connection_type(client):
    filename, content, _ = make_zip_file()
    resp = await client.post(
        "/drivers",
        data={"name": "Bad CT", "connection_type": "InvalidType"},
        files={"file": (filename, io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 422
    assert "connection_type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_update_driver_connection_type(mock_up, client):
    create_resp = await create_driver_package(client, "CT Update", connection_type="Management")
    driver_id = create_resp.json()["id"]
    resp = await client.put(
        f"/drivers/{driver_id}",
        json={"connection_type": "Layer 1 Switch"},
    )
    assert resp.status_code == 200
    assert resp.json()["connection_type"] == "Layer 1 Switch"


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_connection_type_in_response(mock_up, client):
    resp = await create_driver_package(client, "CT Response", connection_type="Layer 2 Switch")
    driver_id = resp.json()["id"]
    get_resp = await client.get(f"/drivers/{driver_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["connection_type"] == "Layer 2 Switch"


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
@patch("app.services.driver_service.download_object", side_effect=mock_download_object)
async def test_internal_download_driver(mock_dl, mock_up, client):
    """Internal download endpoint with valid token returns driver file."""
    create_resp = await create_driver_package(client, "Internal DL Test")
    driver_id = create_resp.json()["id"]
    resp = await client.get(
        f"/drivers/{driver_id}/internal-download",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_internal_download_driver_bad_token(client):
    """Internal download endpoint with bad token returns 403."""
    resp = await client.get(
        f"/drivers/{uuid.uuid4()}/internal-download",
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_download_driver_not_found(client):
    """Internal download endpoint with valid token but nonexistent driver returns 404."""
    resp = await client.get(
        f"/drivers/{uuid.uuid4()}/internal-download",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replace_driver_file_not_found(client):
    """Replace file on nonexistent driver returns 404."""
    resp = await client.put(
        f"/drivers/{uuid.uuid4()}/file",
        files={"file": ("new.zip", io.BytesIO(b"PK\x03\x04test"), "application/zip")},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_dut_only_filters_infrastructure_devices(mock_up, client):
    # Create a Management driver (DUT)
    dut_resp = await create_driver_package(client, "DUT Driver", connection_type="Management")
    dut_driver_id = dut_resp.json()["id"]
    # Create an infrastructure driver
    infra_resp = await create_driver_package(
        client, "Infra Driver", connection_type="Layer 1 Switch"
    )
    infra_driver_id = infra_resp.json()["id"]

    # Create templates
    dut_tmpl = await client.post(
        "/templates",
        json={
            **TEMPLATE_PAYLOAD,
            "name": "DUT Template",
            "driver_id": dut_driver_id,
        },
    )
    infra_tmpl = await client.post(
        "/templates",
        json={
            **TEMPLATE_PAYLOAD,
            "name": "Infra Template",
            "driver_id": infra_driver_id,
        },
    )

    # Create devices
    await client.post(
        "/devices",
        json={
            "name": "DUT Device",
            "template_id": dut_tmpl.json()["id"],
            "topology_type": "PHYSICAL",
            "field_data": {"model": "M1"},
        },
    )
    await client.post(
        "/devices",
        json={
            "name": "Infra Device",
            "template_id": infra_tmpl.json()["id"],
            "topology_type": "PHYSICAL",
            "field_data": {"model": "M2"},
        },
    )

    # Without dut_only: see both
    resp_all = await client.get("/devices")
    assert resp_all.status_code == 200
    all_names = [d["name"] for d in resp_all.json()["items"]]
    assert "DUT Device" in all_names
    assert "Infra Device" in all_names

    # With dut_only=true: only DUT
    resp_dut = await client.get("/devices", params={"dut_only": "true"})
    assert resp_dut.status_code == 200
    dut_names = [d["name"] for d in resp_dut.json()["items"]]
    assert "DUT Device" in dut_names
    assert "Infra Device" not in dut_names


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_non_admin_forced_dut_only(mock_up, client):
    # Create an infrastructure driver + template + device as admin
    infra_resp = await create_driver_package(client, "Infra Only", connection_type="Layer 2 Switch")
    infra_driver_id = infra_resp.json()["id"]
    tmpl = await client.post(
        "/templates",
        json={
            **TEMPLATE_PAYLOAD,
            "name": "Infra Tmpl",
            "driver_id": infra_driver_id,
        },
    )
    await client.post(
        "/devices",
        json={
            "name": "Hidden Device",
            "template_id": tmpl.json()["id"],
            "topology_type": "PHYSICAL",
            "field_data": {"model": "X"},
        },
    )

    # Switch to user auth: should not see infrastructure devices. Mock
    # group-visibility resolution so the list does not reach the (absent) auth
    # service; dut_only is what hides the infra device here, not the visible set.
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.devices._resolve_visible_device_ids",
        new=AsyncMock(return_value=set()),
    ):
        resp = await client.get("/devices")
    assert resp.status_code == 200
    names = [d["name"] for d in resp.json()["items"]]
    assert "Hidden Device" not in names
