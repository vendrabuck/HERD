"""Tests for driver_loader.py: tar.gz extraction, caching, and connection type validation."""

import io
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.services.driver_loader import (
    REQUIRED_METHODS,
    extract_driver_package,
    get_cached_driver,
    validate_driver,
)
from sqlalchemy import select
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


VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def connect_ports(self, a, b): return {"success": True}
    def disconnect_ports(self, a, b): return {"success": True}
    def status(self): return {"reachable": True}
"""

VALID_L2_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def create_vlan(self, vlan_id): return {"success": True}
    def add_to_vlan(self, port, vlan_id, tag="tagged"): return {"success": True}
    def remove_from_vlan(self, port, vlan_id): return {"success": True}
    def delete_vlan(self, vlan_id): return {"success": True}
    def status(self): return {"reachable": True}
"""

VALID_L3_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def configure_route(self): return {"success": True}
    def remove_route(self): return {"success": True}
    def status(self): return {"reachable": True}
"""

VALID_MGMT_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def configure(self): return {"success": True}
    def backup(self): return {"success": True}
    def status(self): return {"reachable": True}
"""


def _make_zip(driver_code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
    return buf.getvalue()


def _make_tar_gz(driver_code: str, extra_files: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = driver_code.encode()
        info = tarfile.TarInfo(name="driver.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        if extra_files:
            for name, content in extra_files.items():
                content_bytes = content.encode()
                info = tarfile.TarInfo(name=name)
                info.size = len(content_bytes)
                tf.addfile(info, io.BytesIO(content_bytes))
    return buf.getvalue()


# --- tar.gz extraction ---


def test_extract_tar_gz():
    tar_bytes = _make_tar_gz(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(tar_bytes, "test.tar.gz", dest)
        assert (dest / "driver.py").exists()


def test_extract_tgz():
    tar_bytes = _make_tar_gz(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(tar_bytes, "test.tgz", dest)
        assert (dest / "driver.py").exists()


def test_extract_tar_gz_with_extra_files():
    extras = {"lib/utils.py": "HELPER = True", "requirements.txt": "requests"}
    tar_bytes = _make_tar_gz(VALID_L1_DRIVER, extras)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(tar_bytes, "test.tar.gz", dest)
        assert (dest / "driver.py").exists()
        assert (dest / "lib" / "utils.py").exists()
        assert (dest / "requirements.txt").exists()


# --- validate all connection types ---


def test_validate_l2_driver():
    zip_bytes = _make_zip(VALID_L2_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 2 Switch")
    assert errors == []


def test_validate_l3_driver():
    zip_bytes = _make_zip(VALID_L3_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 3 Switch")
    assert errors == []


def test_validate_management_driver():
    zip_bytes = _make_zip(VALID_MGMT_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Management")
    assert errors == []


def test_validate_l1_driver_against_l2_type():
    """L1 driver lacks L2 methods (create_vlan, add_to_vlan, remove_from_vlan, delete_vlan)."""
    zip_bytes = _make_zip(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 2 Switch")
    assert len(errors) == 4
    assert any("create_vlan" in e for e in errors)
    assert any("add_to_vlan" in e for e in errors)
    assert any("remove_from_vlan" in e for e in errors)
    assert any("delete_vlan" in e for e in errors)


def test_required_methods_dict_completeness():
    """All connection types have required methods defined."""
    assert "Layer 1 Switch" in REQUIRED_METHODS
    assert "Layer 2 Switch" in REQUIRED_METHODS
    assert "Layer 3 Switch" in REQUIRED_METHODS
    assert "Management" in REQUIRED_METHODS
    # All types require login, logout, status
    for conn_type, methods in REQUIRED_METHODS.items():
        assert "login" in methods, f"{conn_type} missing login"
        assert "logout" in methods, f"{conn_type} missing logout"
        assert "status" in methods, f"{conn_type} missing status"


# --- get_cached_driver ---


@pytest.mark.asyncio
async def test_cache_miss(db):
    """No cache entry returns None."""
    result = await get_cached_driver(db, uuid.uuid4(), "sha256hash")
    assert result is None


@pytest.mark.asyncio
async def test_cache_hit(db):
    """Matching cache entry returns path."""
    driver_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_entry = DriverCache(
            driver_id=driver_id,
            sha256="correct_hash",
            local_path=tmpdir,
        )
        db.add(cache_entry)
        await db.commit()
        result = await get_cached_driver(db, driver_id, "correct_hash")
        assert result == tmpdir


@pytest.mark.asyncio
async def test_cache_sha256_mismatch(db):
    """Stale cache with wrong sha256 is removed and returns None."""
    driver_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_entry = DriverCache(
            driver_id=driver_id,
            sha256="old_hash",
            local_path=tmpdir,
        )
        db.add(cache_entry)
        await db.commit()
        result = await get_cached_driver(db, driver_id, "new_hash")
        assert result is None
        # Cache entry should be deleted
        check = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
        assert check.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_cache_path_missing(db):
    """Cache entry with missing path on disk is cleaned up."""
    driver_id = uuid.uuid4()
    cache_entry = DriverCache(
        driver_id=driver_id,
        sha256="hash123",
        local_path="/nonexistent/path/that/does/not/exist",
    )
    db.add(cache_entry)
    await db.commit()
    result = await get_cached_driver(db, driver_id, "hash123")
    assert result is None
    # Cache entry should be deleted
    check = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    assert check.scalar_one_or_none() is None
