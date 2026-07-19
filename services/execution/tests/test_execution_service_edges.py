"""Edge-branch coverage for execution_service.py.

Covers:
- insert_command_log when every row lacks a command (returns 0, line 124).
- list_execution_runs created_after / created_before filters (lines 160, 162).
- run_driver_action DryRunRefused path (lines 368-380): a refused dry-run is
  recorded as a FAILED run, not propagated.
- run_driver_action command-log persistence failure (lines 385-388): a failing
  insert_command_log is logged and swallowed; the run still succeeds.
- run_driver_action driver-result gating (issue #370): a returned
  {"success": False} records FAILED with the output preserved; a bare-data
  output without a success key stays SUCCESS.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.services import execution_service as ex_service
from app.services.driver_loader import DriverPackageError
from app.services.driver_sandbox import DryRunRefused
from app.services.execution_service import (
    insert_command_log,
    list_execution_runs,
    run_driver_action,
)
from fastapi import HTTPException
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


# --- run_driver_action broken driver package (issue #279) ---


@pytest.mark.asyncio
async def test_run_driver_action_driver_package_error_records_failed(db, monkeypatch):
    """A structurally broken package on the manual-execute path is a FAILED run.

    load_driver raises DriverPackageError for validation failures since issue
    #279 (previously ValueError), so this handler must catch it: an escape here
    would turn a broken package into a 500 instead of a recorded FAILED run.
    """
    monkeypatch.setattr(
        ex_service,
        "load_driver",
        AsyncMock(side_effect=DriverPackageError("Driver validation failed: no Driver class")),
    )

    run = await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "status",
        USER_ID,
    )
    assert run.status == "FAILED"
    assert "Driver validation failed: no Driver class" in run.error


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


# --- run_driver_action driver-result gating (issue #370) ---


@pytest.mark.asyncio
async def test_run_driver_action_driver_result_failure_records_failed(db, monkeypatch):
    """A driver that RETURNS {'success': False} is a FAILED run, not SUCCESS.

    The sandbox transport flag only says the subprocess did not raise; before
    issue #370 this path recorded SUCCESS with the failure buried in output.
    The full output payload must survive on the FAILED row.
    """
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(
            return_value={
                "success": True,
                "output": {"success": False, "error": "mock injected failure on connect_ports"},
                "duration_ms": 5,
            }
        ),
    )

    run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)
    assert run.status == "FAILED"
    assert run.error == "mock injected failure on connect_ports"
    assert run.output is not None
    assert "mock injected failure on connect_ports" in run.output
    assert run.duration_ms == 5


@pytest.mark.asyncio
async def test_run_driver_action_driver_result_failure_without_error_message(db, monkeypatch):
    """A result-level failure with no error string gets the fallback wording."""
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": {"success": False}, "duration_ms": 3}),
    )

    run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)
    assert run.status == "FAILED"
    assert run.error == "driver reported failure"


@pytest.mark.asyncio
async def test_run_driver_action_bare_data_output_stays_success(db, monkeypatch):
    """An output dict with no success key is a bare-data return and stays SUCCESS.

    Conservative posture (issue #370): only an explicit success: False fails the
    run, so drivers returning plain data (frr_mgmt transcripts, status metrics)
    are unaffected by the gate.
    """
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(
            return_value={
                "success": True,
                "output": {"uptime_seconds": 4242, "status": "ok"},
                "duration_ms": 4,
            }
        ),
    )

    run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)
    assert run.status == "SUCCESS"
    assert run.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_success", [0, ""])
async def test_run_driver_action_falsy_success_value_records_failed(db, monkeypatch, falsy_success):
    """A PRESENT success key is judged falsy, not identity-False.

    The sandbox boundary is JSON, so a driver violating the bool contract with
    {"success": 0} or {"success": ""} still crosses as a falsy non-False value;
    the gate must fail these rather than record SUCCESS (review finding on
    issue #370). The bare-data posture is untouched: only a present key fails.
    """
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(
            return_value={
                "success": True,
                "output": {"success": falsy_success},
                "duration_ms": 2,
            }
        ),
    )

    run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)
    assert run.status == "FAILED"
    assert run.error == "driver reported failure"


# --- run_driver_action configure validation: published schema vs registry ---

# A driver-published schema shaped like the FRR Management driver's: it accepts
# raw vtysh lines under `commands`, which the neutral registry Management schema
# (additionalProperties:False over {vlan,ip,hostname,description}) would reject.
_FRR_PUBLISHED_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "command": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
    "minProperties": 1,
}


def _ok_method(**kwargs):
    return MagicMock(return_value={"success": True, "output": None, "duration_ms": 1})()


@pytest.mark.asyncio
async def test_configure_accepts_commands_when_driver_publishes_schema(db, monkeypatch):
    """The blocker fix: with a published schema allowing `commands`, a configure
    call carrying raw vtysh lines passes validation and the run succeeds, where
    the registry-only path would have rejected it as an additional property."""
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "get_driver_config_schema",
        AsyncMock(return_value=_FRR_PUBLISHED_SCHEMA),
    )
    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock(side_effect=_ok_method))

    run = await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "configure",
        USER_ID,
        method_kwargs={"commands": ["ip route 192.0.2.0/24 blackhole"]},
    )
    assert run.status == "SUCCESS"


@pytest.mark.asyncio
async def test_configure_registry_fallback_when_no_published_schema(db, monkeypatch):
    """No published schema => byte-for-byte registry behavior. The registry
    Management schema accepts `hostname` but rejects `commands` with a 422."""
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(ex_service, "get_driver_config_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock(side_effect=_ok_method))

    # hostname is in the registry Management schema, so this passes.
    ok = await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "configure",
        USER_ID,
        method_kwargs={"hostname": "mgmt"},
    )
    assert ok.status == "SUCCESS"

    # commands is not in the registry schema, so this is rejected with a 422 and
    # the run is recorded FAILED.
    with pytest.raises(HTTPException) as exc:
        await run_driver_action(
            db,
            _device_data(),
            _template_data(),
            "configure",
            USER_ID,
            method_kwargs={"commands": ["ip route 192.0.2.0/24 blackhole"]},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_configure_unsafe_published_schema_falls_back_to_registry(db, monkeypatch):
    """A hostile/malformed published schema must never break or bypass
    validation: a non-local $ref raises PublishedSchemaError, the caller falls
    back to the registry. Registry-valid kwargs then pass; registry-invalid
    kwargs are rejected (the published schema cannot smuggle `commands` through)."""
    hostile_schema = {
        "type": "object",
        "properties": {"x": {"$ref": "http://169.254.169.254/latest/meta-data/"}},
    }
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service, "get_driver_config_schema", AsyncMock(return_value=hostile_schema)
    )
    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock(side_effect=_ok_method))

    # Fell back to registry, which accepts hostname.
    ok = await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "configure",
        USER_ID,
        method_kwargs={"hostname": "mgmt"},
    )
    assert ok.status == "SUCCESS"

    # Fell back to registry, which rejects commands: the hostile schema did not
    # widen the vocabulary.
    with pytest.raises(HTTPException) as exc:
        await run_driver_action(
            db,
            _device_data(),
            _template_data(),
            "configure",
            USER_ID,
            method_kwargs={"commands": ["ip route 192.0.2.0/24 blackhole"]},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_configure_validation_runs_after_load_driver(db, monkeypatch):
    """Ordering guard: the published-schema lookup is only correct after
    load_driver has populated the cache for the current SHA. Lock in that
    load_driver is awaited before get_driver_config_schema is read, so a future
    refactor cannot move validation back above the load (which would see a cold
    cache and silently fall back to the registry)."""
    calls = []
    monkeypatch.setattr(
        ex_service,
        "load_driver",
        AsyncMock(side_effect=lambda *a, **k: calls.append("load") or "/tmp/driver"),
    )
    monkeypatch.setattr(
        ex_service,
        "get_driver_config_schema",
        AsyncMock(side_effect=lambda *a, **k: calls.append("schema") or _FRR_PUBLISHED_SCHEMA),
    )
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock(side_effect=_ok_method))

    await run_driver_action(
        db,
        _device_data(),
        _template_data(),
        "configure",
        USER_ID,
        method_kwargs={"commands": ["ip route 192.0.2.0/24 blackhole"]},
    )
    assert calls.index("load") < calls.index("schema")
