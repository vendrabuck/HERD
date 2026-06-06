"""Tests for POST /apply-jobs/{job_id}/confirm (iter 3 Stage 4).

The endpoint promotes a successful dry-run into a real apply by creating
a NEW job, not flipping dry_run on the source row. Tests cover:
- happy path: new pending job with dry_run=False, audit attribution to confirming user
- 409 when source is not a dry-run
- 409 when source dry-run did not succeed (pending, failed, etc)
- 404 when source job does not exist
- ACL gate uses the same widened helper as schedule_apply_job
"""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device_config_apply_job import DeviceConfigApplyJob
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ADMIN_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000001",
    "username": "admin",
    "role": "admin",
}
USER_PAYLOAD = {
    "sub": "00000000-0000-0000-0000-000000000002",
    "username": "viewer",
    "role": "user",
}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


_storage: dict[str, bytes] = {}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mock_upload(key: str, data: bytes, **_: object) -> None:
    _storage[key] = data


def _mock_delete(key: str) -> None:
    _storage.pop(key, None)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=_mock_upload),
        patch("app.services.driver_service.delete_object", side_effect=_mock_delete),
    ):
        yield


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


def _build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", "class Driver:\n    pass\n")
        zf.writestr(
            "driver_metadata.json",
            json.dumps({"supports_dry_run": True, "version": "1.0"}),
        )
    return buf.getvalue()


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_dry_run_job(
    client,
    *,
    status: str = "success",
    dry_run: bool = True,
) -> tuple[str, str, str]:
    """Returns (job_id, device_id, version_id)."""
    global _driver_counter
    _driver_counter += 1
    drv = await client.post(
        "/drivers",
        data={"name": f"CfDrv{_driver_counter}", "connection_type": "Management"},
        files={"file": (f"d{_driver_counter}.zip", io.BytesIO(_build_zip()), "application/zip")},
    )
    assert drv.status_code == 201, drv.text
    tpl = await client.post("/templates", json={**_TEMPLATE, "driver_id": drv.json()["id"]})
    assert tpl.status_code == 201
    dev = await client.post(
        "/devices",
        json={
            "name": f"d-{uuid.uuid4().hex[:6]}",
            "template_id": tpl.json()["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert dev.status_code == 201
    device_id = dev.json()["id"]
    cv = await client.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    version_id = cv.json()["id"]
    sched = await client.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "dry_run": dry_run,
        },
    )
    assert sched.status_code == 201, sched.text
    job_id = sched.json()["id"]

    # Force-update the seeded row's status so we can test promotion gates.
    if status != "pending":
        async with TestSessionLocal() as session:
            row = await session.get(DeviceConfigApplyJob, uuid.UUID(job_id))
            row.status = status
            row.fired_at = datetime.now(timezone.utc)
            await session.commit()

    return job_id, device_id, version_id


# --- Happy path ---


@pytest.mark.asyncio
async def test_confirm_promotes_dry_run_to_real_apply(admin_client):
    job_id, device_id, version_id = await _seed_dry_run_job(admin_client)

    before = datetime.now(timezone.utc)
    resp = await admin_client.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 201, resp.text
    promoted = resp.json()

    # New job, not a flip
    assert promoted["id"] != job_id
    assert promoted["dry_run"] is False
    assert promoted["status"] == "pending"
    assert promoted["device_id"] == device_id
    assert promoted["version_id"] == version_id
    # Confirming user is the audit attribution
    assert promoted["created_by"] == ADMIN_PAYLOAD["sub"]
    assert promoted["author_name"] == ADMIN_PAYLOAD["username"]
    # Scheduled for ~10s in the future. SQLite drops tzinfo on round-trip,
    # so normalize both sides to UTC-aware before subtracting.
    scheduled_for = datetime.fromisoformat(promoted["scheduled_for"])
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    elapsed = (scheduled_for - before).total_seconds()
    assert 5 <= elapsed <= 30


@pytest.mark.asyncio
async def test_confirm_leaves_source_dry_run_intact(admin_client):
    """Promotion creates a new job; the source row is unchanged."""
    job_id, _, _ = await _seed_dry_run_job(admin_client)
    await admin_client.post(f"/apply-jobs/{job_id}/confirm")

    async with TestSessionLocal() as session:
        source = await session.get(DeviceConfigApplyJob, uuid.UUID(job_id))
    assert source.dry_run is True
    assert source.status == "success"


# --- Negative paths ---


@pytest.mark.asyncio
async def test_confirm_404_when_job_missing(admin_client):
    resp = await admin_client.post(f"/apply-jobs/{uuid.uuid4()}/confirm")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_confirm_409_when_source_is_not_dry_run(admin_client):
    """A real-apply job that succeeded cannot be 'promoted', nothing to do."""
    job_id, _, _ = await _seed_dry_run_job(admin_client, dry_run=False)
    resp = await admin_client.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 409
    assert "not a dry-run" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_confirm_409_when_dry_run_pending(admin_client):
    """Source dry-run has not yet fired -> can't promote."""
    job_id, _, _ = await _seed_dry_run_job(admin_client, status="pending")
    resp = await admin_client.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 409
    assert "pending" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_confirm_409_when_dry_run_failed(admin_client):
    """A failed dry-run is not safe to promote; the user has to start over."""
    job_id, _, _ = await _seed_dry_run_job(admin_client, status="failed")
    resp = await admin_client.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 409


# --- ACL gate (reuses the same widened helper as schedule_apply_job) ---


@pytest.mark.asyncio
async def test_confirm_non_admin_without_grant_rejected(admin_client):
    job_id, _, _ = await _seed_dry_run_job(admin_client)

    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    with patch(
        "app.routers.apply_jobs._user_can_manage_device",
        new=AsyncMock(return_value=False),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_confirm_non_admin_owner_allowed(admin_client):
    """Reservation owner can promote (iter-3 widening) just like they could schedule."""
    job_id, _, _ = await _seed_dry_run_job(admin_client)

    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    with patch(
        "app.routers.apply_jobs._user_can_manage_device",
        new=AsyncMock(return_value=True),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(f"/apply-jobs/{job_id}/confirm")
    assert resp.status_code == 201, resp.text
    # Audit attribution is the confirming user, not the original creator.
    assert resp.json()["created_by"] == USER_PAYLOAD["sub"]
