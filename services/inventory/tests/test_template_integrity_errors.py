"""Integrity-error mapping for template writes (issue #277).

Template create/update catch IntegrityError. A unique-name collision must keep
the historical 409 wording; a foreign-key failure (a referenced hypervisor or
driver absent at insert time, now that device_templates.hypervisor_id carries a
real FK) must not be mislabeled as a name conflict. These tests pin both
wordings and exercise the classifier on both database engines the project runs:
asyncpg (Postgres, via a SQLSTATE) in production and aiosqlite (SQLite, via
message text) in unit tests.

The endpoint tests here use a dedicated engine with PRAGMA foreign_keys=ON so
SQLite actually enforces the hypervisor_id FK; the default test engines leave
FK enforcement off.
"""

import io
import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.services.template_service import _integrity_kind
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_fks(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"


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


_SECTIONS = [
    {
        "name": "Instance",
        "fields": [
            {"key": "image", "label": "Image", "type": "string", "required": True},
        ],
    }
]


async def _create_hypervisor_driver(client, name: str) -> str:
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": "Hypervisor"},
        files={"file": ("driver.zip", io.BytesIO(b"PK\x03\x04test"), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- Endpoint-level mapping ---


@pytest.mark.asyncio
async def test_create_duplicate_name_still_409(client):
    payload = {
        "name": "Dup Port Template",
        "template_type": "port",
        "sections": _SECTIONS,
    }
    first = await client.post("/templates", json=payload)
    assert first.status_code == 201, first.text
    second = await client.post("/templates", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"] == "Template with name 'Dup Port Template' already exists"


@pytest.mark.asyncio
async def test_create_fk_violation_is_422_not_name_conflict(client):
    # A dynamic template needs a real Hypervisor driver but points at a
    # hypervisor row that does not exist; the FK fails at insert time.
    driver_id = await _create_hypervisor_driver(client, "Recipe FK")
    resp = await client.post(
        "/templates",
        json={
            "name": "Orphan Dynamic",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": str(uuid.uuid4()),
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail == "Referenced hypervisor or driver does not exist"
    assert "already exists" not in detail


@pytest.mark.asyncio
async def test_create_valid_dynamic_after_fk_failure(client):
    # Sanity: with a real hypervisor the same shape succeeds, proving the 422
    # above is the FK and not a validation reject.
    driver_id = await _create_hypervisor_driver(client, "Recipe OK")
    hv_payload = {
        "name": f"HV-{uuid.uuid4()}",
        "endpoint": "https://pve.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
    }
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200))):
        hv_resp = await client.post("/hypervisors", json=hv_payload)
    assert hv_resp.status_code == 201, hv_resp.text
    resp = await client.post(
        "/templates",
        json={
            "name": "Real Dynamic",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hv_resp.json()["id"],
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text


# --- Classifier unit coverage on both engines ---


def _make_integrity_error(orig) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, orig)


def test_integrity_kind_postgres_sqlstates():
    class _PgOrig:
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

        def __str__(self) -> str:
            return f"asyncpg error {self.sqlstate}"

    assert _integrity_kind(_make_integrity_error(_PgOrig("23505"))) == "unique"
    assert _integrity_kind(_make_integrity_error(_PgOrig("23503"))) == "foreign_key"
    assert _integrity_kind(_make_integrity_error(_PgOrig("23514"))) == "unknown"


def test_integrity_kind_sqlite_message_text():
    unique = sqlite3.IntegrityError("UNIQUE constraint failed: device_templates.name")
    fk = sqlite3.IntegrityError("FOREIGN KEY constraint failed")
    other = sqlite3.IntegrityError("NOT NULL constraint failed: device_templates.sections")
    assert _integrity_kind(_make_integrity_error(unique)) == "unique"
    assert _integrity_kind(_make_integrity_error(fk)) == "foreign_key"
    assert _integrity_kind(_make_integrity_error(other)) == "unknown"
