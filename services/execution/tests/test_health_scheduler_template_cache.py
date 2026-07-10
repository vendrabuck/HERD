"""Unit tests for the health-poll template cache (issue #316).

Fleet-scale polling re-fetched a device's template from inventory on every
single poll; a lab sharing a few templates across many devices multiplied
that into O(polls) redundant inventory calls per interval.
`health_scheduler._fetch_template_cached` adds a short-TTL, in-process cache
keyed by template_id, mirroring the module's existing `_registry` cache
style. These tests exercise the wrapper directly (unit-level) and once
through `fire_poll` (to confirm it is actually wired into the poll path).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from app.database import Base
from app.models.device_health_status import DeviceHealthStatus
from app.services import health_scheduler
from app.services.health_scheduler import _fetch_template_cached, fire_poll
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    health_scheduler._registry.clear()
    health_scheduler._registry_last_refresh = None
    health_scheduler._template_cache.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _template_data():
    return {"sections": []}


def _device_data(template_id: str | None = None):
    return {
        "id": str(uuid.uuid4()),
        "template_id": template_id or str(uuid.uuid4()),
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "abc",
        "driver_filename": "d.zip",
        "connection_type": "Management",
        "field_data": {},
        "name": "dev",
    }


# --- _fetch_template_cached: direct unit tests ---


@pytest.mark.asyncio
async def test_two_calls_within_ttl_fetch_once(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "template_cache_ttl_seconds", 300)
    fetch = AsyncMock(return_value=_template_data())
    monkeypatch.setattr(health_scheduler, "fetch_template", fetch)

    template_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    first = await _fetch_template_cached(template_id, now)
    second = await _fetch_template_cached(template_id, now + timedelta(seconds=1))

    assert first == _template_data()
    assert second == _template_data()
    fetch.assert_called_once_with(template_id)


@pytest.mark.asyncio
async def test_call_after_ttl_expiry_refetches(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "template_cache_ttl_seconds", 5)
    fetch = AsyncMock(return_value=_template_data())
    monkeypatch.setattr(health_scheduler, "fetch_template", fetch)

    template_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await _fetch_template_cached(template_id, now)
    # Still within TTL: no second fetch yet.
    await _fetch_template_cached(template_id, now + timedelta(seconds=4))
    fetch.assert_called_once_with(template_id)

    # Past the TTL: must re-fetch.
    await _fetch_template_cached(template_id, now + timedelta(seconds=6))
    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_different_template_ids_are_cached_independently(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "template_cache_ttl_seconds", 300)
    fetch = AsyncMock(return_value=_template_data())
    monkeypatch.setattr(health_scheduler, "fetch_template", fetch)

    now = datetime.now(timezone.utc)
    template_a = str(uuid.uuid4())
    template_b = str(uuid.uuid4())

    await _fetch_template_cached(template_a, now)
    await _fetch_template_cached(template_b, now)
    await _fetch_template_cached(template_a, now)
    await _fetch_template_cached(template_b, now)

    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_failed_fetch_is_not_cached_as_permanent_negative(monkeypatch):
    """A fetch failure must behave exactly like the pre-cache path: raise,
    and let the next poll try again fresh, never a cached permanent negative.
    """
    monkeypatch.setattr(health_scheduler.settings, "template_cache_ttl_seconds", 300)
    boom = AsyncMock(side_effect=Exception("inventory unreachable"))
    monkeypatch.setattr(health_scheduler, "fetch_template", boom)

    template_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with pytest.raises(Exception, match="inventory unreachable"):
        await _fetch_template_cached(template_id, now)

    # The failure was not cached: the next call attempts a real fetch again,
    # which succeeds once inventory recovers.
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )
    result = await _fetch_template_cached(template_id, now + timedelta(seconds=1))
    assert result == _template_data()


# --- fire_poll: confirm the cache is actually wired into the poll path ---


@pytest.mark.asyncio
async def test_fire_poll_shares_template_cache_across_devices(monkeypatch):
    """Two devices on the same template should cost inventory one template
    fetch total, not one per poll (issue #316's fleet-scale scenario).
    """
    shared_template_id = str(uuid.uuid4())
    device_one = _device_data(template_id=shared_template_id)
    device_two = _device_data(template_id=shared_template_id)
    device_one_id = uuid.UUID(device_one["id"])
    device_two_id = uuid.UUID(device_two["id"])
    now = datetime.now(timezone.utc)

    async with TestSessionLocal() as db:
        db.add(DeviceHealthStatus(device_id=device_one_id, next_poll_at=now))
        db.add(DeviceHealthStatus(device_id=device_two_id, next_poll_at=now))
        await db.commit()

    fetch_device_mock = AsyncMock(side_effect=[device_one, device_two])
    fetch_template_mock = AsyncMock(return_value=_template_data())
    monkeypatch.setattr(health_scheduler, "fetch_device", fetch_device_mock)
    monkeypatch.setattr(health_scheduler, "fetch_template", fetch_template_mock)
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(return_value=_make_success_run()),
    )

    await fire_poll(TestSessionLocal, device_one_id, 60)
    await fire_poll(TestSessionLocal, device_two_id, 60)

    fetch_template_mock.assert_called_once_with(shared_template_id)


def _make_success_run():
    from app.models.execution_run import ExecutionRun

    return ExecutionRun(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        driver_id=uuid.uuid4(),
        driver_sha256="abc",
        action="status",
        status="SUCCESS",
        user_id=health_scheduler.SYSTEM_POLL_USER_ID,
        input_params={},
    )
