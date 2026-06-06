"""Tests for the dry-run path: sandbox refusal, metadata read, transcript shape.

The schedule-endpoint gate that prevents dry-run requests against drivers
without `supports_dry_run: true` is in tests/test_dry_run_gate.py (inventory
service). This file exercises the execution-side safety nets.
"""

import json
import os
import tempfile
import uuid

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.services.driver_loader import (
    DEFAULT_DRIVER_METADATA,
    get_driver_metadata,
    read_driver_metadata,
)
from app.services.driver_sandbox import DryRunRefused, execute_driver_method
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


def _make_driver_dir(driver_code: str, metadata: dict | None = None) -> str:
    tmpdir = tempfile.mkdtemp(prefix="herd_test_drv_")
    with open(os.path.join(tmpdir, "driver.py"), "w") as f:
        f.write(driver_code)
    if metadata is not None:
        with open(os.path.join(tmpdir, "driver_metadata.json"), "w") as f:
            json.dump(metadata, f)
    return tmpdir


DRY_RUN_AWARE_DRIVER = """
try:
    from driver_transcript import record_command
except ImportError:
    def record_command(*args, **kwargs):
        pass


class Driver:
    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        if self.dry_run:
            record_command("set vlan 100", response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        record_command("set vlan 100", response="OK")
        return {"success": True, "simulated": False}

    def backup(self):
        return {"data": ""}

    def status(self):
        return {"reachable": True}
"""


OLDER_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        # Pretends to push for real, ignores context["dry_run"] entirely.
        return {"success": True}

    def backup(self):
        return {"data": ""}

    def status(self):
        return {"reachable": True}
"""


# --- read_driver_metadata: file system parser ---


def test_read_metadata_returns_default_when_missing(tmp_path):
    """No driver_metadata.json -> closed-by-default."""
    assert read_driver_metadata(tmp_path) == DEFAULT_DRIVER_METADATA


def test_read_metadata_returns_parsed_dict(tmp_path):
    meta_file = tmp_path / "driver_metadata.json"
    meta_file.write_text('{"supports_dry_run": true, "vendor": "X"}')
    parsed = read_driver_metadata(tmp_path)
    assert parsed["supports_dry_run"] is True
    assert parsed["vendor"] == "X"


def test_read_metadata_handles_malformed_json(tmp_path):
    meta_file = tmp_path / "driver_metadata.json"
    meta_file.write_text("this is not json")
    assert read_driver_metadata(tmp_path) == DEFAULT_DRIVER_METADATA


def test_read_metadata_handles_non_object(tmp_path):
    meta_file = tmp_path / "driver_metadata.json"
    meta_file.write_text('"a string, not an object"')
    assert read_driver_metadata(tmp_path) == DEFAULT_DRIVER_METADATA


# --- get_driver_metadata: cache row read ---


@pytest.mark.asyncio
async def test_get_metadata_returns_default_when_no_cache_row(db):
    """Unknown driver_id -> default, not exception."""
    meta = await get_driver_metadata(db, uuid.uuid4())
    assert meta == DEFAULT_DRIVER_METADATA


@pytest.mark.asyncio
async def test_get_metadata_returns_default_when_column_empty(db):
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="x",
            local_path="/tmp/x",
            metadata_json=None,
        )
    )
    await db.commit()
    meta = await get_driver_metadata(db, driver_id)
    assert meta == DEFAULT_DRIVER_METADATA


@pytest.mark.asyncio
async def test_get_metadata_returns_parsed_when_set(db):
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="x",
            local_path="/tmp/x",
            metadata_json='{"supports_dry_run": true, "vendor": "Acme"}',
        )
    )
    await db.commit()
    meta = await get_driver_metadata(db, driver_id)
    assert meta["supports_dry_run"] is True
    assert meta["vendor"] == "Acme"


# --- sandbox refusal: defense in depth against unmodified drivers ---


def test_sandbox_refuses_dry_run_when_driver_lacks_metadata():
    """Driver without supports_dry_run claim: refuse before spawn."""
    driver_dir = _make_driver_dir(OLDER_DRIVER, metadata=None)
    with pytest.raises(DryRunRefused):
        execute_driver_method(
            driver_path=driver_dir,
            action="configure",
            context={},
            dry_run=True,
            driver_metadata={},
        )


def test_sandbox_refuses_dry_run_when_metadata_explicitly_false():
    """Driver with supports_dry_run=false: same refusal."""
    driver_dir = _make_driver_dir(OLDER_DRIVER, metadata={"supports_dry_run": False})
    with pytest.raises(DryRunRefused):
        execute_driver_method(
            driver_path=driver_dir,
            action="configure",
            context={},
            dry_run=True,
            driver_metadata={"supports_dry_run": False},
        )


def test_sandbox_allows_dry_run_when_metadata_true():
    """Driver advertising support runs in dry-run, transcript marks simulated."""
    driver_dir = _make_driver_dir(DRY_RUN_AWARE_DRIVER, metadata={"supports_dry_run": True})
    result = execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={},
        dry_run=True,
        driver_metadata={"supports_dry_run": True},
    )
    assert result["success"] is True
    output = result["output"]
    assert output["simulated"] is True
    transcript = result["transcript"]
    assert len(transcript) == 1
    assert transcript[0]["exit_status"] == "simulated"
    assert transcript[0]["response"] == "(simulated)"


def test_sandbox_real_run_skips_metadata_check():
    """dry_run=false is the default; metadata is irrelevant."""
    driver_dir = _make_driver_dir(OLDER_DRIVER, metadata=None)
    result = execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={},
        # dry_run defaults to False; driver_metadata not required.
    )
    assert result["success"] is True


def test_sandbox_does_not_mutate_caller_context_on_dry_run():
    """Injecting dry_run into context should not leak into the caller's dict."""
    driver_dir = _make_driver_dir(DRY_RUN_AWARE_DRIVER, metadata={"supports_dry_run": True})
    caller_context = {"HERD_ip_address": "10.0.0.1"}
    execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context=caller_context,
        dry_run=True,
        driver_metadata={"supports_dry_run": True},
    )
    assert "dry_run" not in caller_context


def test_sandbox_dry_run_aware_driver_real_run_records_real_response():
    """Same driver in real mode emits the real response, not '(simulated)'."""
    driver_dir = _make_driver_dir(DRY_RUN_AWARE_DRIVER, metadata={"supports_dry_run": True})
    result = execute_driver_method(
        driver_path=driver_dir,
        action="configure",
        context={},
        dry_run=False,
    )
    output = result["output"]
    assert output["simulated"] is False
    transcript = result["transcript"]
    assert len(transcript) == 1
    assert transcript[0]["response"] == "OK"
    assert transcript[0]["exit_status"] == "ok"


# --- load_driver populates the cache metadata column ---


@pytest.mark.asyncio
async def test_load_driver_populates_cached_metadata(db, monkeypatch, tmp_path):
    """A package containing driver_metadata.json should populate the cache row."""
    from app.services import driver_loader

    # Stub the download and extract: we just want load_driver to find the
    # metadata file we wrote ourselves.
    driver_id = uuid.uuid4()
    sha = "abc"
    dest = tmp_path / str(driver_id)
    dest.mkdir(parents=True)
    (dest / "driver.py").write_text(DRY_RUN_AWARE_DRIVER)
    (dest / "driver_metadata.json").write_text('{"supports_dry_run": true}')

    async def fake_download(_driver_id):
        return b""

    def fake_extract(_pkg, _filename, dest_dir):
        # No-op: we pre-populated dest above.
        return

    monkeypatch.setattr(driver_loader, "download_driver_package", fake_download)
    monkeypatch.setattr(driver_loader, "extract_driver_package", fake_extract)
    monkeypatch.setattr(driver_loader.settings, "driver_cache_path", str(tmp_path))

    await driver_loader.load_driver(
        db,
        driver_id=driver_id,
        driver_sha256=sha,
        driver_filename="d.zip",
        connection_type="Management",
    )

    meta = await get_driver_metadata(db, driver_id)
    assert meta["supports_dry_run"] is True
