"""RBAC denial sweep: every require_admin endpoint in the inventory service
returns 403 for an authenticated non-admin caller.

The auth layer is `make_auth_dependencies`; when `get_current_user_payload`
returns a payload with role='user', the transitive `require_admin` dependency
rejects with 403 before any handler logic runs.
"""

import io
import uuid

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_user():
    return {"sub": USER_UUID, "username": "regular", "role": "user"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# Valid-shape payloads so body parsing passes; require_admin fires first but
# some Pydantic parses run eagerly. Using minimal valid dicts keeps this robust.
DEVICE_ID = str(uuid.uuid4())
PORT_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
GROUP_ID = str(uuid.uuid4())


DEVICE_BODY = {"name": "d1", "template_id": TEMPLATE_ID, "fields": {}}
PORT_BODY = {"name": "eth0", "direction": "bidirectional"}
BULK_PORT_BODY = {"ports": [{"name": "eth0", "direction": "bidirectional"}]}
TEMPLATE_BODY = {
    "name": "T1",
    "driver_id": DRIVER_ID,
    "vendor": "V",
    "model": "M",
    "sections": [{"name": "General", "fields": [{"key": "m", "label": "M", "type": "string"}]}],
}
DRIVER_UPDATE_BODY = {"name": "n", "description": "d", "connection_type": "Management"}
GROUP_BODY = {"name": "g1", "description": "d"}
BULK_IDS_BODY = {"device_ids": [str(uuid.uuid4())]}
BULK_UG_BODY = {"user_group_ids": [str(uuid.uuid4())]}


ADMIN_ENDPOINTS_JSON: list[tuple[str, str, dict | None]] = [
    # devices
    ("POST", "/devices", DEVICE_BODY),
    ("PUT", f"/devices/{DEVICE_ID}", DEVICE_BODY),
    ("DELETE", f"/devices/{DEVICE_ID}", None),
    # ports
    ("POST", f"/devices/{DEVICE_ID}/ports", PORT_BODY),
    ("POST", f"/devices/{DEVICE_ID}/ports/bulk", BULK_PORT_BODY),
    ("PUT", f"/ports/{PORT_ID}", PORT_BODY),
    ("DELETE", f"/ports/{PORT_ID}", None),
    # templates
    ("POST", "/templates", TEMPLATE_BODY),
    ("PUT", f"/templates/{TEMPLATE_ID}", TEMPLATE_BODY),
    ("DELETE", f"/templates/{TEMPLATE_ID}", None),
    # drivers (metadata/delete only, file-upload handled separately)
    ("PUT", f"/drivers/{DRIVER_ID}", DRIVER_UPDATE_BODY),
    ("DELETE", f"/drivers/{DRIVER_ID}", None),
    # device groups
    ("POST", "/device-groups", GROUP_BODY),
    ("GET", "/device-groups", None),
    ("GET", f"/device-groups/{GROUP_ID}", None),
    ("PUT", f"/device-groups/{GROUP_ID}", GROUP_BODY),
    ("DELETE", f"/device-groups/{GROUP_ID}", None),
    ("POST", f"/device-groups/{GROUP_ID}/devices/bulk", BULK_IDS_BODY),
    ("POST", f"/device-groups/{GROUP_ID}/devices/bulk-remove", BULK_IDS_BODY),
    ("POST", f"/device-groups/{GROUP_ID}/permissions/bulk", BULK_UG_BODY),
    ("POST", f"/device-groups/{GROUP_ID}/permissions/bulk-remove", BULK_UG_BODY),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS_JSON)
@pytest.mark.asyncio
async def test_non_admin_denied_json(user_client, method, path, body):
    if body is None:
        resp = await user_client.request(method, path)
    else:
        resp = await user_client.request(method, path, json=body)
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_non_admin_denied_driver_upload(user_client):
    resp = await user_client.post(
        "/drivers",
        data={"name": "zzz", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_denied_driver_file_replace(user_client):
    resp = await user_client.put(
        f"/drivers/{DRIVER_ID}/file",
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert resp.status_code == 403
