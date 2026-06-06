"""Tests for the poll_interval_seconds column on devices and templates,
plus the /devices/health-config internal endpoint that drives the
execution service's health-poll scheduler.
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
    from unittest.mock import patch

    storage: dict[str, bytes] = {}

    def upload(key: str, data: bytes, content_type: str = "") -> None:
        storage[key] = data

    def delete(key: str) -> None:
        storage.pop(key, None)

    with (
        patch("app.services.driver_service.upload_object", side_effect=upload),
        patch("app.services.driver_service.delete_object", side_effect=delete),
    ):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_driver_counter = 0


async def _make_driver(client) -> str:
    global _driver_counter
    _driver_counter += 1
    resp = await client.post(
        "/drivers",
        data={"name": f"d{_driver_counter}", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _make_template(client, *, poll_interval_seconds: int | None = None) -> str:
    driver_id = await _make_driver(client)
    payload = {
        "name": f"tmpl-{uuid.uuid4()}",
        "driver_id": driver_id,
        "vendor": "Juniper",
        "model": "EX3300",
        "sections": [
            {
                "name": "General",
                "fields": [{"key": "f1", "label": "F1", "type": "string"}],
            }
        ],
    }
    if poll_interval_seconds is not None:
        payload["poll_interval_seconds"] = poll_interval_seconds
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_device(
    client,
    template_id: str,
    *,
    name: str | None = None,
    poll_interval_seconds: int | None = None,
) -> dict:
    payload = {
        "name": name or f"dev-{uuid.uuid4()}",
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {"f1": "x"},
    }
    if poll_interval_seconds is not None:
        payload["poll_interval_seconds"] = poll_interval_seconds
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Schema persistence ---


@pytest.mark.asyncio
async def test_create_device_with_poll_interval_persists(client):
    tid = await _make_template(client)
    body = await _make_device(client, tid, poll_interval_seconds=120)
    assert body["poll_interval_seconds"] == 120
    assert body["resolved_poll_interval_seconds"] == 120


@pytest.mark.asyncio
async def test_create_device_without_poll_interval_inherits_from_template(client):
    tid = await _make_template(client, poll_interval_seconds=300)
    body = await _make_device(client, tid)
    assert body["poll_interval_seconds"] is None
    assert body["resolved_poll_interval_seconds"] == 300


@pytest.mark.asyncio
async def test_create_device_with_no_poll_interval_anywhere_is_unpolled(client):
    tid = await _make_template(client)
    body = await _make_device(client, tid)
    assert body["poll_interval_seconds"] is None
    assert body["resolved_poll_interval_seconds"] is None


@pytest.mark.asyncio
async def test_device_poll_interval_overrides_template(client):
    tid = await _make_template(client, poll_interval_seconds=300)
    body = await _make_device(client, tid, poll_interval_seconds=60)
    assert body["poll_interval_seconds"] == 60
    assert body["resolved_poll_interval_seconds"] == 60


@pytest.mark.asyncio
async def test_update_device_can_clear_poll_interval_to_null(client):
    tid = await _make_template(client)
    body = await _make_device(client, tid, poll_interval_seconds=120)
    device_id = body["id"]
    update = await client.put(
        f"/devices/{device_id}",
        json={"poll_interval_seconds": None},
    )
    assert update.status_code == 200
    assert update.json()["poll_interval_seconds"] is None


@pytest.mark.asyncio
async def test_update_template_poll_interval_persists(client):
    tid = await _make_template(client)
    update = await client.put(
        f"/templates/{tid}",
        json={"poll_interval_seconds": 600},
    )
    assert update.status_code == 200, update.text
    assert update.json()["poll_interval_seconds"] == 600


# --- Validation ---


@pytest.mark.asyncio
async def test_poll_interval_below_minimum_rejected_on_device(client):
    tid = await _make_template(client)
    resp = await client.post(
        "/devices",
        json={
            "name": "dev-low",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"f1": "x"},
            "poll_interval_seconds": 10,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_poll_interval_below_minimum_rejected_on_template(client):
    driver_id = await _make_driver(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "tmpl-low",
            "driver_id": driver_id,
            "vendor": "X",
            "model": "Y",
            "sections": [{"name": "S", "fields": [{"key": "f", "label": "F", "type": "string"}]}],
            "poll_interval_seconds": 5,
        },
    )
    assert resp.status_code == 422


# --- Health-config endpoint ---


@pytest.mark.asyncio
async def test_health_config_returns_only_devices_with_resolved_interval(client):
    # Two templates: one with a default interval, one without.
    tid_polled = await _make_template(client, poll_interval_seconds=300)
    tid_unpolled = await _make_template(client)

    # Device A: inherits 300 from template.
    a = await _make_device(client, tid_polled)
    # Device B: explicit 120, overrides template default.
    b = await _make_device(client, tid_polled, poll_interval_seconds=120)
    # Device C: unpolled template, no explicit interval -> should NOT appear.
    await _make_device(client, tid_unpolled)
    # Device D: unpolled template, explicit 60 -> should appear with 60.
    d = await _make_device(client, tid_unpolled, poll_interval_seconds=60)

    resp = await client.get(
        "/devices/health-config",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    by_device = {row["device_id"]: row["resolved_interval_seconds"] for row in rows}
    assert by_device[a["id"]] == 300
    assert by_device[b["id"]] == 120
    assert by_device[d["id"]] == 60
    # Three rows total; Device C absent.
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_health_config_requires_internal_token(client):
    resp = await client.get(
        "/devices/health-config",
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_health_config_route_not_swallowed_by_device_id_path(client):
    """Sanity: health-config must be matched before /devices/{device_id}.

    If route ordering regresses, this endpoint would try to parse
    'health-config' as a UUID and fail differently.
    """
    resp = await client.get(
        "/devices/health-config",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json() == []
