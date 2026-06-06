"""Tests for the per-command transcript pipeline: subprocess capture,
DB persistence, and the GET /runs/{id}/commands read endpoint.

ACL coverage lives in test_command_log_acl.py.
"""

import os
import tempfile
import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.execution_command import ExecutionCommand
from app.models.execution_run import ExecutionRun
from app.routers.executions import (
    _require_internal_token,
    get_current_user_payload,
    require_admin,
)
from app.services.driver_sandbox import execute_driver_method
from app.services.execution_service import (
    create_execution_run,
    insert_command_log,
    list_command_log,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _make_driver_dir(driver_code: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="herd_test_driver_")
    with open(os.path.join(tmpdir, "driver.py"), "w") as f:
        f.write(driver_code)
    return tmpdir


# Drivers MUST be standalone modules with `class Driver`. The runner adds its
# own directory to sys.path so `from driver_transcript import record_command`
# works inside the subprocess.
RECORDING_DRIVER = """
from driver_transcript import record_command


class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        record_command("vlan 100", response="OK", duration_ms=5)
        record_command("interface eth1", response="OK", exit_status="ok")
        return {"success": True}

    def backup(self):
        return {"data": ""}

    def status(self):
        return {"reachable": True}
"""


SILENT_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        return {"success": True}

    def backup(self):
        return {"data": ""}

    def status(self):
        return {"reachable": True}
"""


def test_sandbox_captures_transcript():
    """Driver that calls record_command emits rows the parent reads back."""
    driver_dir = _make_driver_dir(RECORDING_DRIVER)
    result = execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={"HERD_ip_address": "10.0.0.1"},
    )
    assert result["success"] is True
    transcript = result.get("transcript")
    assert isinstance(transcript, list)
    assert len(transcript) == 2
    assert transcript[0]["command"] == "vlan 100"
    assert transcript[0]["response"] == "OK"
    assert transcript[0]["duration_ms"] == 5
    assert transcript[0]["exit_status"] == "ok"
    assert transcript[1]["command"] == "interface eth1"


def test_sandbox_empty_transcript_when_driver_silent():
    """Driver that does not call record_command produces an empty transcript list."""
    driver_dir = _make_driver_dir(SILENT_DRIVER)
    result = execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={},
    )
    assert result["success"] is True
    assert result.get("transcript") == []


def test_sandbox_cleans_up_transcript_file():
    """The transcript JSONL temp file is unlinked after the subprocess exits."""
    driver_dir = _make_driver_dir(RECORDING_DRIVER)
    before = set(os.listdir(tempfile.gettempdir()))
    execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={},
    )
    after = set(os.listdir(tempfile.gettempdir()))
    leaked = [n for n in (after - before) if n.startswith("herd_tx_")]
    assert leaked == [], f"transcript files leaked: {leaked}"


async def _create_run(db, **overrides) -> uuid.UUID:
    defaults = {
        "device_id": uuid.uuid4(),
        "driver_id": uuid.uuid4(),
        "driver_sha256": "sha",
        "action": "configure",
        "user_id": uuid.uuid4(),
        "input_params": {},
    }
    defaults.update(overrides)
    run = await create_execution_run(db, **defaults)
    return run.id


@pytest.mark.asyncio
async def test_insert_command_log_inserts_rows_in_order(db):
    run_id = await _create_run(db)
    rows = [
        {"command": "vlan 100", "response": "OK", "duration_ms": 3, "exit_status": "ok"},
        {"command": "interface eth1", "response": "OK"},
        {"command": "switchport mode trunk"},
    ]
    count = await insert_command_log(db, run_id, rows)
    assert count == 3
    listed = await list_command_log(db, run_id)
    assert [r.seq for r in listed] == [1, 2, 3]
    assert listed[0].command == "vlan 100"
    assert listed[0].response == "OK"
    assert listed[0].duration_ms == 3
    assert listed[1].command == "interface eth1"
    assert listed[1].duration_ms is None
    assert listed[2].exit_status == "ok"  # default applied when row omits it


@pytest.mark.asyncio
async def test_insert_command_log_skips_rows_missing_command(db):
    run_id = await _create_run(db)
    rows = [
        {"command": "vlan 100"},
        {"response": "no command field"},  # skipped
        {"command": "interface eth1"},
    ]
    count = await insert_command_log(db, run_id, rows)
    assert count == 2
    listed = await list_command_log(db, run_id)
    assert [r.command for r in listed] == ["vlan 100", "interface eth1"]
    # seq must stay contiguous (1..N) even though a row was skipped; a naive
    # enumerate over all rows would leave a gap (here it would yield [1, 3]).
    assert [r.seq for r in listed] == [1, 2]


@pytest.mark.asyncio
async def test_insert_command_log_empty_rows_is_noop(db):
    run_id = await _create_run(db)
    assert await insert_command_log(db, run_id, []) == 0


@pytest.mark.asyncio
async def test_list_command_log_empty_for_run_with_no_rows(db):
    run_id = await _create_run(db)
    assert await list_command_log(db, run_id) == []


@pytest.mark.asyncio
async def test_get_run_commands_admin(admin_client):
    """Admin can GET /runs/{id}/commands and sees ordered rows."""
    async with TestSessionLocal() as session:
        run_id = await _create_run(session)
        await insert_command_log(
            session,
            run_id,
            [
                {"command": "first", "response": "OK"},
                {"command": "second", "response": "OK"},
            ],
        )
    resp = await admin_client.get(f"/runs/{run_id}/commands")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [r["seq"] for r in body] == [1, 2]
    assert body[0]["command"] == "first"
    assert body[1]["command"] == "second"


@pytest.mark.asyncio
async def test_get_run_commands_not_found(admin_client):
    """Unknown run_id returns 404, not an empty list."""
    resp = await admin_client.get(f"/runs/{uuid.uuid4()}/commands")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_command_rows_cascade_on_run_delete(db):
    """Deleting a run cascades to its command-log rows (FK ON DELETE CASCADE)."""
    run_id = await _create_run(db)
    await insert_command_log(db, run_id, [{"command": "x"}])
    assert len(await list_command_log(db, run_id)) == 1

    await db.execute(delete(ExecutionRun).where(ExecutionRun.id == run_id))
    await db.commit()
    # SQLite enforces FK cascades only when PRAGMA foreign_keys=ON is set on
    # the connection. Verify the model FK definition declares CASCADE so the
    # behavior holds against Postgres in production.
    fk = next(iter(ExecutionCommand.__table__.foreign_keys))
    assert fk.ondelete == "CASCADE"
