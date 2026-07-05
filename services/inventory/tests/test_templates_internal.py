"""Internal template endpoint: driver_sha256 + driver_filename exposure (issue #32).

The execution service loads a template's recipe/driver package from the internal
template response, which needs the driver's sha256 and filename alongside
driver_id and connection_type (load_driver's four inputs). These two tests pin
that GET /templates/{id}/internal derives both fields through template.driver,
exactly as the device internal response does.
"""

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

_INTERNAL = {"X-Internal-Token": "test-token"}
_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"

_SECTIONS = [
    {
        "name": "Instance",
        "fields": [
            {"key": "image", "label": "Image", "type": "string", "required": True},
        ],
    }
]


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with patch("app.services.driver_service.upload_object", side_effect=lambda *a, **k: None):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _create_driver(client, connection_type: str, name: str) -> dict:
    content = b"PK\x03\x04test"
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": connection_type},
        files={"file": ("recipe.zip", io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_hypervisor(client) -> str:
    payload = {
        "name": f"HV-{uuid.uuid4()}",
        "endpoint": "https://pve.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
    }
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200))):
        resp = await client.post("/hypervisors", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_internal_template_exposes_driver_sha256_and_filename(client):
    """A dynamic template's internal response carries its recipe driver's
    sha256 and filename, derived through template.driver."""
    driver = await _create_driver(client, "Hypervisor", "Recipe SHA")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "Linux VM",
            "template_type": "dynamic",
            "driver_id": driver["id"],
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    template_id = resp.json()["id"]

    got = await client.get(f"/templates/{template_id}/internal", headers=_INTERNAL)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["driver_id"] == driver["id"]
    assert body["connection_type"] == "Hypervisor"
    # The two fields under test, matched against the driver row's own values.
    assert body["driver_sha256"] == driver["sha256"]
    assert body["driver_filename"] == driver["filename"]


@pytest.mark.asyncio
async def test_internal_template_null_driver_fields_for_port_template(client):
    """A port template has no driver, so both derived fields stay null rather
    than raising."""
    resp = await client.post(
        "/templates",
        json={
            "name": "Bare Port",
            "template_type": "port",
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    template_id = resp.json()["id"]

    got = await client.get(f"/templates/{template_id}/internal", headers=_INTERNAL)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["driver_sha256"] is None
    assert body["driver_filename"] is None
