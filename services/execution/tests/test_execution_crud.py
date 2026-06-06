"""Tests for execution_service.py database CRUD operations."""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from app.database import Base
from app.services.execution_service import (
    create_execution_run,
    get_execution_run,
    list_execution_runs,
    update_execution_run,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


DEVICE_ID = uuid.uuid4()
DRIVER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
RESERVATION_ID = uuid.uuid4()


async def _create_run(db, **overrides):
    defaults = {
        "device_id": DEVICE_ID,
        "driver_id": DRIVER_ID,
        "driver_sha256": "abc123",
        "action": "login",
        "user_id": USER_ID,
        "input_params": {"HERD_ip_address": "10.0.1.50"},
    }
    defaults.update(overrides)
    return await create_execution_run(db, **defaults)


# --- create_execution_run ---


@pytest.mark.asyncio
async def test_create_execution_run(db):
    run = await _create_run(db)
    assert run.id is not None
    assert run.device_id == DEVICE_ID
    assert run.driver_id == DRIVER_ID
    assert run.driver_sha256 == "abc123"
    assert run.action == "login"
    assert run.status == "PENDING"
    assert run.user_id == USER_ID
    assert run.input_params == {"HERD_ip_address": "10.0.1.50"}
    assert run.port_a is None
    assert run.port_b is None


@pytest.mark.asyncio
async def test_create_execution_run_with_ports(db):
    run = await _create_run(db, action="connect_ports", port_a="1/1/1", port_b="1/1/2")
    assert run.action == "connect_ports"
    assert run.port_a == "1/1/1"
    assert run.port_b == "1/1/2"


@pytest.mark.asyncio
async def test_create_execution_run_with_reservation(db):
    run = await _create_run(db, reservation_id=RESERVATION_ID)
    assert run.reservation_id == RESERVATION_ID


# --- update_execution_run ---


@pytest.mark.asyncio
async def test_update_run_status(db):
    run = await _create_run(db)
    assert run.status == "PENDING"
    updated = await update_execution_run(db, run, status="RUNNING")
    assert updated.status == "RUNNING"


@pytest.mark.asyncio
async def test_update_run_success(db):
    run = await _create_run(db)
    now = datetime.now(timezone.utc)
    updated = await update_execution_run(
        db,
        run,
        status="SUCCESS",
        output='{"result": true}',
        started_at=now,
        completed_at=now,
        duration_ms=150,
    )
    assert updated.status == "SUCCESS"
    assert updated.output == '{"result": true}'
    assert updated.duration_ms == 150
    assert updated.started_at is not None
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_update_run_failure(db):
    run = await _create_run(db)
    updated = await update_execution_run(
        db,
        run,
        status="FAILED",
        error="Connection refused",
    )
    assert updated.status == "FAILED"
    assert updated.error == "Connection refused"


@pytest.mark.asyncio
async def test_update_run_timeout(db):
    run = await _create_run(db)
    updated = await update_execution_run(
        db,
        run,
        status="TIMEOUT",
        error="Execution timed out after 30s",
    )
    assert updated.status == "TIMEOUT"
    assert "timed out" in updated.error


@pytest.mark.asyncio
async def test_update_preserves_existing_fields(db):
    """Fields not passed to update remain unchanged."""
    run = await _create_run(db)
    updated = await update_execution_run(
        db,
        run,
        status="RUNNING",
        started_at=datetime.now(timezone.utc),
    )
    # Now update status without passing started_at again
    final = await update_execution_run(db, updated, status="SUCCESS")
    assert final.status == "SUCCESS"
    assert final.started_at is not None  # preserved from previous update


# --- get_execution_run ---


@pytest.mark.asyncio
async def test_get_execution_run(db):
    run = await _create_run(db)
    fetched = await get_execution_run(db, run.id)
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.action == "login"


@pytest.mark.asyncio
async def test_get_execution_run_not_found(db):
    fetched = await get_execution_run(db, uuid.uuid4())
    assert fetched is None


# --- list_execution_runs ---


@pytest.mark.asyncio
async def test_list_execution_runs_empty(db):
    items, total = await list_execution_runs(db)
    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_execution_runs_returns_all(db):
    await _create_run(db, action="login")
    await asyncio.sleep(1.1)  # SQLite timestamp resolution
    await _create_run(db, action="status")
    items, total = await list_execution_runs(db)
    assert total == 2
    assert len(items) == 2
    # Ordered by created_at desc
    assert items[0].action == "status"
    assert items[1].action == "login"


@pytest.mark.asyncio
async def test_list_filter_by_device_id(db):
    other_device = uuid.uuid4()
    await _create_run(db, device_id=DEVICE_ID)
    await _create_run(db, device_id=other_device)
    items, total = await list_execution_runs(db, device_id=DEVICE_ID)
    assert total == 1
    assert items[0].device_id == DEVICE_ID


@pytest.mark.asyncio
async def test_list_filter_by_reservation_id(db):
    await _create_run(db, reservation_id=RESERVATION_ID)
    await _create_run(db)  # no reservation
    items, total = await list_execution_runs(db, reservation_id=RESERVATION_ID)
    assert total == 1
    assert items[0].reservation_id == RESERVATION_ID


@pytest.mark.asyncio
async def test_list_filter_by_status(db):
    run1 = await _create_run(db)
    await update_execution_run(db, run1, status="SUCCESS")
    await _create_run(db)  # stays PENDING
    items, total = await list_execution_runs(db, status_filter="SUCCESS")
    assert total == 1
    assert items[0].status == "SUCCESS"


@pytest.mark.asyncio
async def test_list_pagination(db):
    for i in range(5):
        await _create_run(db, action=f"action_{i}")
        await asyncio.sleep(1.1)  # SQLite timestamp resolution
    items, total = await list_execution_runs(db, skip=2, limit=2)
    assert total == 5
    assert len(items) == 2


@pytest.mark.asyncio
async def test_list_combined_filters(db):
    """Multiple filters applied together."""
    run = await _create_run(
        db,
        device_id=DEVICE_ID,
        reservation_id=RESERVATION_ID,
    )
    await update_execution_run(db, run, status="SUCCESS")
    await _create_run(db, device_id=DEVICE_ID)  # different reservation, PENDING
    items, total = await list_execution_runs(
        db,
        device_id=DEVICE_ID,
        reservation_id=RESERVATION_ID,
        status_filter="SUCCESS",
    )
    assert total == 1
