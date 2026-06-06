"""Unit tests for template_service.py."""

import uuid

import pytest
from app.database import Base
from app.models.device import Device
from app.models.driver_package import DriverPackage
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.template import SectionDefinition, TemplateCreate, TemplateUpdate
from app.services.template_service import (
    create_template,
    delete_template,
    get_template,
    list_templates,
    update_template,
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


async def _create_driver(db):
    driver = DriverPackage(
        name="TestDriver",
        connection_type="Management",
        filename="test.zip",
        storage_key="test/test.zip",
        size_bytes=100,
        sha256="abc123",
        uploaded_by="admin",
    )
    db.add(driver)
    await db.commit()
    await db.refresh(driver)
    return driver


def _make_section():
    return SectionDefinition(name="Net", fields=[])


# --- create_template ---


@pytest.mark.asyncio
async def test_create_template_success():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        data = TemplateCreate(
            name="T1",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[_make_section()],
        )
        tmpl = await create_template(db, data)
        assert tmpl.name == "T1"
        assert tmpl.template_type == "device"


@pytest.mark.asyncio
async def test_create_template_duplicate_name():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        data = TemplateCreate(
            name="Dup",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[_make_section()],
        )
        await create_template(db, data)
        with pytest.raises(HTTPException) as exc:
            await create_template(db, data)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_port_template():
    async with TestSessionLocal() as db:
        data = TemplateCreate(
            name="PortT",
            template_type="port",
            sections=[_make_section()],
        )
        tmpl = await create_template(db, data)
        assert tmpl.template_type == "port"


# --- get_template ---


@pytest.mark.asyncio
async def test_get_template_not_found():
    async with TestSessionLocal() as db:
        result = await get_template(db, uuid.uuid4())
        assert result is None


# --- update_template ---


@pytest.mark.asyncio
async def test_update_template_not_found():
    async with TestSessionLocal() as db:
        data = TemplateUpdate(name="New")
        result = await update_template(db, uuid.uuid4(), data)
        assert result is None


@pytest.mark.asyncio
async def test_update_template_with_sections():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        data = TemplateCreate(
            name="T1",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[_make_section()],
        )
        tmpl = await create_template(db, data)
        new_section = SectionDefinition(name="Updated", fields=[])
        update_data = TemplateUpdate(sections=[new_section])
        updated = await update_template(db, tmpl.id, update_data)
        assert updated.sections[0]["name"] == "Updated"


@pytest.mark.asyncio
async def test_update_template_duplicate_name():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        await create_template(
            db,
            TemplateCreate(
                name="Taken",
                template_type="device",
                vendor="V",
                model="M",
                driver_id=driver.id,
                sections=[_make_section()],
            ),
        )
        tmpl2 = await create_template(
            db,
            TemplateCreate(
                name="Other",
                template_type="device",
                vendor="V",
                model="M",
                driver_id=driver.id,
                sections=[_make_section()],
            ),
        )
        with pytest.raises(HTTPException) as exc:
            await update_template(db, tmpl2.id, TemplateUpdate(name="Taken"))
        assert exc.value.status_code == 409


# --- delete_template ---


@pytest.mark.asyncio
async def test_delete_template_not_found():
    async with TestSessionLocal() as db:
        result = await delete_template(db, uuid.uuid4())
        assert result is False


@pytest.mark.asyncio
async def test_delete_template_success():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        data = TemplateCreate(
            name="Del",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[_make_section()],
        )
        tmpl = await create_template(db, data)
        result = await delete_template(db, tmpl.id)
        assert result is True


@pytest.mark.asyncio
async def test_delete_template_with_device_refs():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        data = TemplateCreate(
            name="InUse",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[_make_section()],
        )
        tmpl = await create_template(db, data)
        device = Device(
            name="Dev1",
            template_id=tmpl.id,
            topology_type="PHYSICAL",
            field_data={},
        )
        db.add(device)
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await delete_template(db, tmpl.id)
        assert exc.value.status_code == 409
        assert "devices still reference" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_template_with_port_refs():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = DeviceTemplate(
            name="DevT",
            template_type="device",
            vendor="V",
            model="M",
            driver_id=driver.id,
            sections=[],
        )
        db.add(dev_tmpl)
        await db.commit()
        await db.refresh(dev_tmpl)

        port_data = TemplateCreate(
            name="PortInUse",
            template_type="port",
            sections=[_make_section()],
        )
        port_tmpl = await create_template(db, port_data)

        device = Device(
            name="Dev1",
            template_id=dev_tmpl.id,
            topology_type="PHYSICAL",
            field_data={},
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)

        port = Port(
            name="eth0",
            device_id=device.id,
            template_id=port_tmpl.id,
            field_data={},
        )
        db.add(port)
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await delete_template(db, port_tmpl.id)
        assert exc.value.status_code == 409
        assert "ports still reference" in exc.value.detail


# --- list_templates ---


@pytest.mark.asyncio
async def test_list_templates_pagination():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        for i in range(4):
            await create_template(
                db,
                TemplateCreate(
                    name=f"T{i}",
                    template_type="device",
                    vendor="V",
                    model="M",
                    driver_id=driver.id,
                    sections=[_make_section()],
                ),
            )
        templates, total = await list_templates(db, skip=1, limit=2)
        assert len(templates) == 2
        assert total == 4


@pytest.mark.asyncio
async def test_list_templates_filter_type():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        await create_template(
            db,
            TemplateCreate(
                name="Dev",
                template_type="device",
                vendor="V",
                model="M",
                driver_id=driver.id,
                sections=[_make_section()],
            ),
        )
        await create_template(
            db,
            TemplateCreate(
                name="Port",
                template_type="port",
                sections=[_make_section()],
            ),
        )
        devices, total = await list_templates(db, template_type="device")
        assert total == 1
        assert devices[0].name == "Dev"
