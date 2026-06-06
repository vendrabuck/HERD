"""Tests for the dry_run gate on POST /devices/{id}/config-versions/{vid}/schedule.

Two layers cover the safety contract:
- The inventory schedule endpoint rejects dry_run=true against drivers whose
  uploaded package did not declare `supports_dry_run: true` in
  driver_metadata.json (this file).
- The execution sandbox refuses to spawn the subprocess in the same case as
  defense in depth (services/execution/tests/test_dry_run.py).
"""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


def _build_driver_zip(supports_dry_run: bool | None) -> bytes:
    """Build a tiny zip containing driver.py and optionally driver_metadata.json.

    supports_dry_run=None means omit the metadata file entirely (default closed).
    True / False bake the flag into the metadata blob.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", "class Driver:\n    pass\n")
        if supports_dry_run is not None:
            zf.writestr(
                "driver_metadata.json",
                json.dumps({"supports_dry_run": supports_dry_run, "version": "1.0"}),
            )
    return buf.getvalue()


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


async def _create_driver_with_metadata(client, supports_dry_run: bool | None) -> str:
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"DryDrv{_driver_counter}", "connection_type": "Management"},
        files={
            "file": (
                "d.zip",
                io.BytesIO(_build_driver_zip(supports_dry_run)),
                "application/zip",
            )
        },
    )
    assert drv.status_code == 201, drv.text
    return drv.json()["id"]


async def _create_device(client, supports_dry_run: bool | None) -> tuple[str, str]:
    """Returns (device_id, driver_id)."""
    driver_id = await _create_driver_with_metadata(client, supports_dry_run)
    tpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": driver_id})
    assert tpl.status_code == 201, tpl.text
    template_id = tpl.json()["id"]
    dev = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "FW-400"},
        },
    )
    assert dev.status_code == 201, dev.text
    return dev.json()["id"], driver_id


def _future(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# --- driver-upload metadata parse ---


@pytest.mark.asyncio
async def test_uploaded_driver_with_supports_dry_run_true_persists_flag(client):
    drv_id = await _create_driver_with_metadata(client, True)
    resp = await client.get(f"/drivers/{drv_id}")
    assert resp.status_code == 200
    assert resp.json()["supports_dry_run"] is True


@pytest.mark.asyncio
async def test_uploaded_driver_without_metadata_defaults_to_false(client):
    drv_id = await _create_driver_with_metadata(client, None)
    resp = await client.get(f"/drivers/{drv_id}")
    assert resp.status_code == 200
    assert resp.json()["supports_dry_run"] is False


@pytest.mark.asyncio
async def test_uploaded_driver_with_supports_dry_run_false_stays_false(client):
    drv_id = await _create_driver_with_metadata(client, False)
    resp = await client.get(f"/drivers/{drv_id}")
    assert resp.status_code == 200
    assert resp.json()["supports_dry_run"] is False


# --- schedule endpoint gate ---


@pytest.mark.asyncio
async def test_schedule_dry_run_succeeds_against_supporting_driver(client):
    device_id, _ = await _create_device(client, supports_dry_run=True)
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    assert cv.status_code == 201
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": True},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_schedule_dry_run_rejected_against_non_supporting_driver(client):
    device_id, _ = await _create_device(client, supports_dry_run=False)
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": True},
    )
    assert resp.status_code == 422
    assert "dry-run" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_dry_run_rejected_when_metadata_absent(client):
    device_id, _ = await _create_device(client, supports_dry_run=None)
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_default_is_not_dry_run(client):
    """Existing callers that omit dry_run should keep working (default false)."""
    device_id, _ = await _create_device(client, supports_dry_run=False)
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future()},
    )
    assert resp.status_code == 201
    assert resp.json()["dry_run"] is False


@pytest.mark.asyncio
async def test_schedule_dry_run_false_against_non_supporting_driver_allowed(client):
    """Real-apply requests against legacy drivers must NOT trip the gate."""
    device_id, _ = await _create_device(client, supports_dry_run=False)
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    vid = cv.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/config-versions/{vid}/schedule",
        json={"scheduled_for": _future(), "dry_run": False},
    )
    assert resp.status_code == 201


# --- scheduler-side: dry_run is forwarded to execution ---


@pytest.mark.asyncio
async def test_scheduler_forwards_dry_run_to_execution():
    """The apply scheduler's _post_internal_execute must include dry_run in the body."""
    from unittest.mock import AsyncMock

    from app.models.device_config_apply_job import DeviceConfigApplyJob
    from app.services.apply_scheduler import _post_internal_execute

    job = DeviceConfigApplyJob(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        scheduled_for=datetime.now(timezone.utc),
        status="pending",
        created_by=uuid.uuid4(),
        dry_run=True,
    )

    captured = {}

    class _FakeResp:
        status_code = 201

        def json(self):
            return {"id": str(uuid.uuid4()), "status": "SUCCESS"}

    async def _capture_post(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        return _FakeResp()

    fake_client = AsyncMock()
    fake_client.post = _capture_post

    await _post_internal_execute(fake_client, job, config={"vlan": 100})

    assert "dry_run" in captured["body"]
    assert captured["body"]["dry_run"] is True
