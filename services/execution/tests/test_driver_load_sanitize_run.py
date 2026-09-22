"""End-to-end proof that a driver-LOAD failure never stores foreign exception
text on the ExecutionRun row (issue #870, the "still storing str(exc)" item
CLAUDE.md's driver-packages section calls out).

run_driver_action's `except (DriverPackageError, ValueError, RuntimeError) as
e:` branch (execution_service.py) stores `str(e)` on the run row verbatim; the
sanitizing has to happen at driver_loader.py's raise sites (proven in
isolation by test_driver_loader_load.py), but this file drives the REAL
load_driver through the REAL run_driver_action so a regression at either layer
shows up here as a leaked sentinel on the persisted row, not just on a raised
exception's __str__.
"""

import tempfile
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.services.execution_service import run_driver_action
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

DEVICE_ID = uuid.uuid4()
DRIVER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

SENTINEL = "http://secret-internal-host/leak"


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
        "connection_type": "Layer 1 Switch",
        "field_data": {},
        "name": "dev",
    }


def _template_data() -> dict:
    return {"sections": []}


def _make_zip(driver_code: str) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_run_driver_action_download_failure_stores_class_name_only(db, caplog):
    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(side_effect=Exception(f"boom at {SENTINEL}")),
        ),
        caplog.at_level("ERROR"),
    ):
        run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)

    assert run.status == "FAILED"
    assert SENTINEL not in (run.error or "")
    assert run.error.endswith("Exception")

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(SENTINEL in line for line in formatted)


@pytest.mark.asyncio
async def test_run_driver_action_extraction_failure_stores_class_name_only(db, caplog):
    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=b"not a zip"),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
            patch(
                "app.services.driver_loader.extract_driver_package",
                side_effect=zipfile.BadZipFile(f"boom at {SENTINEL}"),
            ),
            caplog.at_level("ERROR"),
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)

    assert run.status == "FAILED"
    assert SENTINEL not in (run.error or "")
    assert run.error.endswith("BadZipFile")

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(SENTINEL in line for line in formatted)


@pytest.mark.asyncio
async def test_run_driver_action_validate_import_failure_stores_class_name_only(db, caplog):
    driver_code = f"raise RuntimeError({SENTINEL!r})\n"
    zip_bytes = _make_zip(driver_code)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
            caplog.at_level("ERROR"),
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            run = await run_driver_action(db, _device_data(), _template_data(), "status", USER_ID)

    assert run.status == "FAILED"
    assert SENTINEL not in (run.error or "")
    assert run.error == "Driver validation failed: Failed to load driver.py: RuntimeError"

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(SENTINEL in line for line in formatted)
