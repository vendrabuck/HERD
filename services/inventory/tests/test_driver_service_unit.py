"""Unit tests for driver_service.py."""

import uuid
from unittest.mock import patch

import pytest
from app.database import Base
from app.models.template import DeviceTemplate
from app.services.driver_service import (
    _validate_connection_type,
    _validate_filename,
    create_driver,
    delete_driver,
    download_driver,
    get_driver,
    list_drivers,
    replace_driver_file,
    update_driver,
)
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# --- Validation ---


def test_validate_filename_zip():
    _validate_filename("driver.zip")  # should not raise


def test_validate_filename_tar_gz():
    _validate_filename("driver.tar.gz")  # should not raise


def test_validate_filename_invalid():
    with pytest.raises(HTTPException) as exc:
        _validate_filename("driver.exe")
    assert exc.value.status_code == 422


def test_validate_connection_type_valid():
    _validate_connection_type("Management")  # should not raise
    _validate_connection_type("Layer 1 Switch")


def test_validate_connection_type_invalid():
    with pytest.raises(HTTPException) as exc:
        _validate_connection_type("InvalidType")
    assert exc.value.status_code == 422


# --- create_driver ---


@pytest.mark.asyncio
async def test_create_driver_success():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "Test",
                "desc",
                "Management",
                "test.zip",
                b"zipdata",
                "admin",
                10_000_000,
            )
            assert driver.name == "Test"
            assert driver.connection_type == "Management"


@pytest.mark.asyncio
async def test_create_driver_file_too_large():
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await create_driver(
                db,
                "Big",
                "desc",
                "Management",
                "big.zip",
                b"x" * 1001,
                "admin",
                1000,
            )
        assert exc.value.status_code == 422
        assert "too large" in exc.value.detail


@pytest.mark.asyncio
async def test_create_driver_duplicate_name():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            with patch("app.services.driver_service.delete_object"):
                await create_driver(
                    db,
                    "Dup",
                    "desc",
                    "Management",
                    "dup.zip",
                    b"data",
                    "admin",
                    10_000_000,
                )
                with pytest.raises(HTTPException) as exc:
                    await create_driver(
                        db,
                        "Dup",
                        "other",
                        "Management",
                        "dup2.zip",
                        b"data",
                        "admin",
                        10_000_000,
                    )
                assert exc.value.status_code == 409


# --- get_driver ---


@pytest.mark.asyncio
async def test_get_driver_not_found():
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await get_driver(db, uuid.uuid4())
        assert exc.value.status_code == 404


# --- update_driver ---


@pytest.mark.asyncio
async def test_update_driver_metadata():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "Orig",
                "desc",
                "Management",
                "test.zip",
                b"data",
                "admin",
                10_000_000,
            )
            updated = await update_driver(db, driver.id, name="Renamed", description="new desc")
            assert updated.name == "Renamed"
            assert updated.description == "new desc"


@pytest.mark.asyncio
async def test_update_driver_connection_type():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "Drv",
                "desc",
                "Management",
                "test.zip",
                b"data",
                "admin",
                10_000_000,
            )
            updated = await update_driver(
                db,
                driver.id,
                name=None,
                description=None,
                connection_type="Layer 1 Switch",
            )
            assert updated.connection_type == "Layer 1 Switch"


@pytest.mark.asyncio
async def test_update_driver_invalid_connection_type():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "Drv",
                "desc",
                "Management",
                "test.zip",
                b"data",
                "admin",
                10_000_000,
            )
            with pytest.raises(HTTPException) as exc:
                await update_driver(
                    db,
                    driver.id,
                    name=None,
                    description=None,
                    connection_type="BadType",
                )
            assert exc.value.status_code == 422


# --- replace_driver_file ---


@pytest.mark.asyncio
async def test_replace_driver_file_success():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            with patch("app.services.driver_service.delete_object"):
                driver = await create_driver(
                    db,
                    "Drv",
                    "desc",
                    "Management",
                    "old.zip",
                    b"old",
                    "admin",
                    10_000_000,
                )
                updated = await replace_driver_file(
                    db,
                    driver.id,
                    "new.zip",
                    b"newdata",
                    "admin",
                    10_000_000,
                )
                assert updated.filename == "new.zip"
                assert updated.size_bytes == len(b"newdata")


# --- delete_driver ---


@pytest.mark.asyncio
async def test_delete_driver_success():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            with patch("app.services.driver_service.delete_object"):
                driver = await create_driver(
                    db,
                    "Del",
                    "desc",
                    "Management",
                    "del.zip",
                    b"data",
                    "admin",
                    10_000_000,
                )
                await delete_driver(db, driver.id)
                with pytest.raises(HTTPException):
                    await get_driver(db, driver.id)


@pytest.mark.asyncio
async def test_delete_driver_with_template_refs():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "InUse",
                "desc",
                "Management",
                "in.zip",
                b"data",
                "admin",
                10_000_000,
            )
            # Create a template referencing this driver
            tmpl = DeviceTemplate(
                name="T1",
                template_type="device",
                driver_id=driver.id,
                sections=[],
            )
            db.add(tmpl)
            await db.commit()
            with pytest.raises(HTTPException) as exc:
                await delete_driver(db, driver.id)
            assert exc.value.status_code == 409
            assert "templates still reference" in exc.value.detail


# --- list_drivers ---


@pytest.mark.asyncio
async def test_list_drivers_empty():
    async with TestSessionLocal() as db:
        drivers, total = await list_drivers(db)
        assert drivers == []
        assert total == 0


@pytest.mark.asyncio
async def test_list_drivers_pagination():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            for i in range(3):
                await create_driver(
                    db,
                    f"D{i}",
                    "desc",
                    "Management",
                    f"d{i}.zip",
                    b"data",
                    "admin",
                    10_000_000,
                )
            drivers, total = await list_drivers(db, skip=1, limit=1)
            assert len(drivers) == 1
            assert total == 3


# --- download_driver ---


@pytest.mark.asyncio
async def test_download_driver_success():
    async with TestSessionLocal() as db:
        with patch("app.services.driver_service.upload_object"):
            driver = await create_driver(
                db,
                "Dl",
                "desc",
                "Management",
                "dl.zip",
                b"filedata",
                "admin",
                10_000_000,
            )
            with patch("app.services.driver_service.download_object", return_value=b"filedata"):
                pkg, data = await download_driver(db, driver.id)
                assert data == b"filedata"
                assert pkg.id == driver.id
