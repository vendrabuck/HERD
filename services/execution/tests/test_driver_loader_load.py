"""Tests for driver_loader.py: load_driver full flow and edge cases."""

import io
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.services.driver_loader import (
    download_driver_package,
    extract_driver_package,
    load_driver,
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


def _make_zip(driver_code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
    return buf.getvalue()


# --- extract_driver_package edge cases ---


def test_extract_unsupported_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        with pytest.raises(ValueError, match="Unsupported package format"):
            extract_driver_package(b"data", "driver.exe", dest)


# --- download_driver_package ---


@pytest.mark.asyncio
async def test_download_driver_package_success():
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"zipdata"
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.driver_loader.httpx.AsyncClient", return_value=mock_client):
        result = await download_driver_package(uuid.uuid4())
    assert result == b"zipdata"


@pytest.mark.asyncio
async def test_download_driver_package_failure():
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.driver_loader.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(Exception, match="404"):
            await download_driver_package(uuid.uuid4())


# --- load_driver full flow ---


@pytest.mark.asyncio
async def test_load_driver_cache_hit(db):
    """Cache hit returns cached path without download."""
    driver_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a valid driver
        (Path(tmpdir) / "driver.py").write_text(VALID_L1_DRIVER)
        cache = DriverCache(driver_id=driver_id, sha256="abc", local_path=tmpdir)
        db.add(cache)
        await db.commit()

        result = await load_driver(db, driver_id, "abc", "driver.zip", "Layer 1 Switch")
        assert result == tmpdir


@pytest.mark.asyncio
async def test_load_driver_download_and_cache(db):
    """Cache miss downloads, extracts, validates, and caches."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            result = await load_driver(db, driver_id, "sha256", "driver.zip", "Layer 1 Switch")
        assert Path(result).exists()
        assert (Path(result) / "driver.py").exists()

        # Verify cache entry was created
        check = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
        assert check.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_load_driver_download_failure(db):
    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(side_effect=Exception("Connection refused")),
        ),
    ):
        with pytest.raises(RuntimeError, match="Failed to download"):
            await load_driver(db, uuid.uuid4(), "sha", "d.zip", "Layer 1 Switch")


@pytest.mark.asyncio
async def test_load_driver_extraction_failure(db):
    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=b"not a zip"),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            with pytest.raises(RuntimeError, match="Failed to extract"):
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")


@pytest.mark.asyncio
async def test_load_driver_validation_failure(db):
    """Driver zip without required methods raises ValueError."""
    bad_driver = """
class Driver:
    def __init__(self, context): pass
    def login(self): pass
"""
    zip_bytes = _make_zip(bad_driver)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            with pytest.raises(ValueError, match="validation failed"):
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")


@pytest.mark.asyncio
async def test_load_driver_updates_existing_cache(db):
    """If cache entry exists with different sha, it gets updated on new download."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    # Pre-insert stale cache entry (will be removed by get_cached_driver sha mismatch)
    cache = DriverCache(driver_id=driver_id, sha256="old_sha", local_path="/nonexistent")
    db.add(cache)
    await db.commit()

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            result = await load_driver(db, driver_id, "new_sha", "driver.zip", "Layer 1 Switch")
        assert Path(result).exists()
