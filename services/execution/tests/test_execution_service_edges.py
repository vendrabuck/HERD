"""Edge-branch coverage for execution_service.py.

Covers:
- insert_command_log when every row lacks a command (returns 0, line 124).
- list_execution_runs created_after / created_before filters (lines 160, 162).
- run_driver_action DryRunRefused path (lines 368-380): a refused dry-run is
  recorded as a FAILED run, not propagated.
- run_driver_action command-log persistence failure (lines 385-388): a failing
  insert_command_log is logged and swallowed; the run still succeeds.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.services import execution_service as ex_service
from app.services.driver_sandbox import DryRunRefused
from app.services.execution_service import (
    insert_command_log,
    list_execution_runs,
    run_driver_action,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

DEVICE_ID = uuid.uuid4()
DRIVER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


def _device_data() -> dict:
    return {
        "id": str(DEVICE_ID),
        "driver_id": str(DRIVER_ID),
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Management",
        "field_data": {},
        "name": "dev",
    }


def _template_data() -> dict:
    return {"sections": []}


# --- insert_command_log: all rows skipped (line 124) ---


@pytest.mark.asyncio
async def test_insert_command_log_all_rows_without_command_returns_zero(db):
    run = ExecutionRun(
        device_id=DEVICE_ID,
        driver_id=DRIVER_ID,
        driver_sha256="sha",
        action="status",
        user_id=USER_ID,
        status="SUCCESS",
        input_params={},
    )
    db.add(run)
    await db.commit()

    # Every row lacks a "command" key, so all are skipped and the count is 0.
    rows = [{"response": "x"}, {"command": ""}, {"duration_ms": 5}]
    count = await insert_command_log(db, run.id, rows)
    assert count == 0


# --- list_execution_runs date filters (lines 160, 162) ---


@pytest.mark.asyncio
async def test_list_execution_runs_created_after_and_before(db):
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    for offset_days in (0, 5, 10):
        run = ExecutionRun(
            device_id=DEVICE_ID,
            driver_id=DRIVER_ID,
            driver_sha256="sha",
            action="status",
            user_id=USER_ID,
            status="SUCCESS",
            input_params={},
            created_at=base + timedelta(days=offset_days),
        )
        db.add(run)
    await db.commit()

    # created_after keeps the day-5 and day-10 runs.
    items, total = await list_execution_runs(db, created_after=base + timedelta(days=1))
    assert total == 2

    # created_before keeps the day-0 run only (strict <).
    items, total = await list_execution_runs(db, created_before=base + timedelta(days=1))
    assert total == 1

    # A window that brackets only the day-5 run.
    items, total = await list_execution_runs(
        db,
        created_after=base + timedelta(days=1),
        created_before=base + timedelta(days=8),
    )
    assert total == 1

    # device_id and status filters narrow the result set too.
    items, total = await list_execution_runs(db, device_id=DEVICE_ID)
    assert total == 3
    items, total = await list_execution_runs(db, device_id=uuid.uuid4())
    assert total == 0
    items, total = await list_execution_runs(db, status_filter="SUCCESS")
    assert total == 3
    items, total = await list_execution_runs(db, status_filter="FAILED")
    assert total == 0


# --- run_driver_action DryRunRefused (lines 368-380) ---


@pytest.mark.asyncio
async def test_run_driver_action_dry_run_refused_records_failed(db, monkeypatch):
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))

    def _refuse(**kwargs):
        raise DryRunRefused("driver does not advertise dry-run support")

    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock(side_effect=_refuse))

    run = await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "status",
        USER_ID,
        dry_run=True,
    )
    assert run.status == "FAILED"
    assert "dry-run refused" in run.error


# --- run_driver_action command-log persistence failure (lines 385-388) ---


@pytest.mark.asyncio
async def test_run_driver_action_command_log_failure_swallowed(db, monkeypatch):
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(
            return_value={
                "success": True,
                "output": {"ok": True},
                "duration_ms": 7,
                "transcript": [{"command": "show version"}],
            }
        ),
    )
    # The transcript persist raises; run_driver_action must swallow it and the
    # run must still be SUCCESS.
    monkeypatch.setattr(
        ex_service,
        "insert_command_log",
        AsyncMock(side_effect=RuntimeError("insert failed")),
    )

    run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)
    assert run.status == "SUCCESS"
    assert run.duration_ms == 7
