"""Unit tests for GET /devices/{device_id}/apply-jobs/internal.

Internal-token-gated summary feeding the AI orchestrator's purpose
classifier (issue #646 phase 2): names and a count only, never job
contents or configs. See app/routers/apply_jobs.py for the endpoint and
docs/AI_PURPOSE_CLASSIFICATION.md for the consumer side.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

TOKEN = "test-internal-token"


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_job(
    db,
    device_id: uuid.UUID,
    *,
    description: str | None,
    version_number: int,
) -> None:
    version = DeviceConfigVersion(
        device_id=device_id,
        version_number=version_number,
        connection_type="Management",
        config={"secret": "should-never-be-read-here"},
        description=description,
        created_by=uuid.uuid4(),
        author_name="tester",
    )
    db.add(version)
    await db.flush()
    job = DeviceConfigApplyJob(
        device_id=device_id,
        version_id=version.id,
        scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=1),
        status="pending",
        created_by=uuid.uuid4(),
        author_name="tester",
    )
    db.add(job)


@pytest.mark.asyncio
async def test_summary_returns_count_and_deduplicated_names(client):
    device_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        await _seed_job(db, device_id, description="apply-x", version_number=1)
        await _seed_job(db, device_id, description="apply-x", version_number=2)
        await _seed_job(db, device_id, description="apply-y", version_number=3)
        await db.commit()

    resp = await client.get(
        f"/devices/{device_id}/apply-jobs/internal",
        headers={"X-Internal-Token": TOKEN},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert sorted(body["names"]) == ["apply-x", "apply-y"]


@pytest.mark.asyncio
async def test_summary_omits_null_descriptions_but_still_counts_them(client):
    device_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        await _seed_job(db, device_id, description=None, version_number=1)
        await db.commit()

    resp = await client.get(
        f"/devices/{device_id}/apply-jobs/internal",
        headers={"X-Internal-Token": TOKEN},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["names"] == []


@pytest.mark.asyncio
async def test_summary_never_leaks_config_contents(client):
    """The description is a human label, never the raw config JSON; assert
    the secret-shaped config value never appears in the response body."""
    device_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        await _seed_job(db, device_id, description="apply-x", version_number=1)
        await db.commit()

    resp = await client.get(
        f"/devices/{device_id}/apply-jobs/internal",
        headers={"X-Internal-Token": TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert "should-never-be-read-here" not in resp.text


@pytest.mark.asyncio
async def test_summary_empty_for_unknown_device(client):
    resp = await client.get(
        f"/devices/{uuid.uuid4()}/apply-jobs/internal",
        headers={"X-Internal-Token": TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 0, "names": []}


@pytest.mark.asyncio
async def test_summary_requires_internal_token(client):
    device_id = uuid.uuid4()
    # Header(...) is required, so a missing header 422s before the token
    # comparison runs at all (same shape as every other internal route in
    # this service); a present-but-wrong token is the 403 case.
    resp = await client.get(f"/devices/{device_id}/apply-jobs/internal")
    assert resp.status_code == 422

    resp = await client.get(
        f"/devices/{device_id}/apply-jobs/internal",
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403
