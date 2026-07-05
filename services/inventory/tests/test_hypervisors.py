import io
import uuid
from unittest.mock import AsyncMock, patch

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

# Where the secret-existence HTTP call is made; patched per test to drive the
# 200 (exists) / 404 (missing) / transport-error / 5xx branches.
_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


def override_auth_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "testuser", "role": "user"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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


def _secret_ok():
    """Patch the secret-existence call to report the secret exists (200)."""
    return patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200)))


def _hypervisor_payload(**overrides) -> dict:
    payload = {
        "name": "Proxmox A",
        "description": "Primary lab hypervisor",
        "endpoint": "https://pve.lab.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
        "enabled": True,
    }
    payload.update(overrides)
    return payload


# --- CRUD happy paths ---


@pytest.mark.asyncio
async def test_create_hypervisor(client):
    with _secret_ok():
        resp = await client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Proxmox A"
    assert data["hypervisor_type"] == "proxmox"
    assert data["endpoint"] == "https://pve.lab.example:8006"
    assert data["enabled"] is True
    # The audit column is threaded from the acting admin.
    assert data["modified_by"] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_list_and_get_hypervisor(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    listing = await client.get("/hypervisors")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    one = await client.get(f"/hypervisors/{hid}")
    assert one.status_code == 200
    assert one.json()["id"] == hid


@pytest.mark.asyncio
async def test_update_hypervisor_without_secret_change_skips_validation(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    # No secret_id in the body: validation must not be invoked. Patch it to blow
    # up if called, proving the update path leaves the secret check untouched.
    with patch(_SECRET_GET, new=AsyncMock(side_effect=AssertionError("should not validate"))):
        resp = await client.put(f"/hypervisors/{hid}", json={"description": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated"


@pytest.mark.asyncio
async def test_update_hypervisor_changed_secret_revalidates(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    new_secret = str(uuid.uuid4())
    with _secret_ok():
        resp = await client.put(f"/hypervisors/{hid}", json={"secret_id": new_secret})
    assert resp.status_code == 200
    assert resp.json()["secret_id"] == new_secret


@pytest.mark.asyncio
async def test_delete_hypervisor(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    resp = await client.delete(f"/hypervisors/{hid}")
    assert resp.status_code == 204
    assert (await client.get(f"/hypervisors/{hid}")).status_code == 404


# --- Secret-existence validation ---


@pytest.mark.asyncio
async def test_create_hypervisor_missing_secret_422(client):
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(404))):
        resp = await client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Secret does not exist"


@pytest.mark.asyncio
async def test_create_hypervisor_secrets_transport_error_503(client):
    with patch(_SECRET_GET, new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = await client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "secrets service unreachable while validating secret"


@pytest.mark.asyncio
async def test_create_hypervisor_secrets_5xx_503(client):
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(500))):
        resp = await client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "secrets service returned 500 while validating secret"


# --- Delete-block when a template references the hypervisor ---


async def _create_hypervisor_driver(client, name: str) -> str:
    content = b"PK\x03\x04test"
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": "Hypervisor"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


_DYNAMIC_SECTIONS = [
    {
        "name": "Instance",
        "fields": [
            {"key": "image", "label": "Image", "type": "string", "required": True},
            {"key": "cpu", "label": "CPU", "type": "number", "default": 2},
        ],
    }
]


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=lambda *a, **k: None)
async def test_delete_hypervisor_blocked_by_template(mock_up, client):
    with _secret_ok():
        hv = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = hv.json()["id"]
    driver_id = await _create_hypervisor_driver(client, "Recipe Driver")
    tmpl = await client.post(
        "/templates",
        json={
            "name": "Linux VM",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _DYNAMIC_SECTIONS,
        },
    )
    assert tmpl.status_code == 201
    resp = await client.delete(f"/hypervisors/{hid}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Cannot delete hypervisor: templates still reference it"


# --- Not found and duplicate ---


@pytest.mark.asyncio
async def test_get_hypervisor_not_found(client):
    resp = await client.get(f"/hypervisors/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_hypervisor_not_found(client):
    resp = await client.delete(f"/hypervisors/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_hypervisor_duplicate_name_409(client):
    with _secret_ok():
        await client.post("/hypervisors", json=_hypervisor_payload())
    with _secret_ok():
        resp = await client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


# --- Permissions ---


@pytest.mark.asyncio
async def test_user_cannot_create_hypervisor(user_client):
    with _secret_ok():
        resp = await user_client.post("/hypervisors", json=_hypervisor_payload())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_list_hypervisors(user_client):
    resp = await user_client.get("/hypervisors")
    assert resp.status_code == 403


# --- Internal endpoint ---


@pytest.mark.asyncio
async def test_internal_get_hypervisor(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    resp = await client.get(
        f"/hypervisors/{hid}/internal", headers={"X-Internal-Token": "test-token"}
    )
    assert resp.status_code == 200
    data = resp.json()
    # Exactly the ADR-pinned field set.
    assert set(data.keys()) == {"id", "name", "endpoint", "hypervisor_type", "secret_id", "enabled"}
    assert data["id"] == hid


@pytest.mark.asyncio
async def test_internal_get_hypervisor_bad_token_403(client):
    with _secret_ok():
        create = await client.post("/hypervisors", json=_hypervisor_payload())
    hid = create.json()["id"]
    resp = await client.get(f"/hypervisors/{hid}/internal", headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


@pytest.mark.asyncio
async def test_internal_get_hypervisor_not_found(client):
    resp = await client.get(
        f"/hypervisors/{uuid.uuid4()}/internal", headers={"X-Internal-Token": "test-token"}
    )
    assert resp.status_code == 404
