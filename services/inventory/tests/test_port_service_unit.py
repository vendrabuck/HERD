"""Unit tests for port_service.py."""

import uuid

import pytest
from app.database import Base
from app.models.device import Device
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.port import BulkPortCreate, PortCreate, PortUpdate
from app.services.port_service import (
    create_port,
    create_ports_bulk,
    delete_port,
    get_port,
    list_ports,
    update_port,
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


async def _create_device_template(db, driver_id):
    tmpl = DeviceTemplate(
        name="DeviceT",
        template_type="device",
        driver_id=driver_id,
        sections=[{"name": "Net", "fields": [{"key": "ip", "type": "string", "label": "IP"}]}],
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _create_port_template(db):
    tmpl = DeviceTemplate(
        name="PortT",
        template_type="port",
        sections=[{"name": "Info", "fields": []}],
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _create_device(db, template_id):
    device = Device(
        name="Dev1",
        template_id=template_id,
        topology_type="PHYSICAL",
        field_data={"ip": "10.0.0.1"},
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


@pytest.mark.asyncio
async def test_create_port_device_not_found():
    async with TestSessionLocal() as db:
        port_tmpl = await _create_port_template(db)
        data = PortCreate(name="eth0", template_id=port_tmpl.id, field_data={})
        with pytest.raises(HTTPException) as exc:
            await create_port(db, uuid.uuid4(), data)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_port_template_not_found():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        data = PortCreate(name="eth0", template_id=uuid.uuid4(), field_data={})
        with pytest.raises(HTTPException) as exc:
            await create_port(db, device.id, data)
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_port_wrong_template_type():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        data = PortCreate(name="eth0", template_id=dev_tmpl.id, field_data={})
        with pytest.raises(HTTPException) as exc:
            await create_port(db, device.id, data)
        assert exc.value.status_code == 422
        assert "not a port template" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_port_success():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        port_tmpl = await _create_port_template(db)
        data = PortCreate(name="eth0", template_id=port_tmpl.id, field_data={})
        port = await create_port(db, device.id, data)
        assert port.name == "eth0"
        assert port.device_id == device.id


@pytest.mark.asyncio
async def test_update_port_not_found():
    async with TestSessionLocal() as db:
        data = PortUpdate(name="eth1")
        result = await update_port(db, uuid.uuid4(), data)
        assert result is None


@pytest.mark.asyncio
async def test_update_port_success():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        port_tmpl = await _create_port_template(db)
        data = PortCreate(name="eth0", template_id=port_tmpl.id, field_data={})
        port = await create_port(db, device.id, data)
        updated = await update_port(db, port.id, PortUpdate(name="eth99"))
        assert updated.name == "eth99"


@pytest.mark.asyncio
async def test_create_ports_bulk_device_not_found():
    async with TestSessionLocal() as db:
        port_tmpl = await _create_port_template(db)
        data = BulkPortCreate(
            name_prefix="eth",
            starting_index=1,
            instances=3,
            template_id=port_tmpl.id,
            field_data={},
        )
        with pytest.raises(HTTPException) as exc:
            await create_ports_bulk(db, uuid.uuid4(), data)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_ports_bulk_naming():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        port_tmpl = await _create_port_template(db)
        data = BulkPortCreate(
            name_prefix="eth",
            starting_index=1,
            instances=3,
            template_id=port_tmpl.id,
            field_data={},
        )
        ports = await create_ports_bulk(db, device.id, data)
        names = [p.name for p in ports]
        assert names == ["eth1", "eth2", "eth3"]


@pytest.mark.asyncio
async def test_delete_port_not_found():
    async with TestSessionLocal() as db:
        result = await delete_port(db, uuid.uuid4())
        assert result is False


@pytest.mark.asyncio
async def test_delete_port_success():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        port_tmpl = await _create_port_template(db)
        data = PortCreate(name="eth0", template_id=port_tmpl.id, field_data={})
        port = await create_port(db, device.id, data)
        result = await delete_port(db, port.id)
        assert result is True
        assert await get_port(db, port.id) is None


@pytest.mark.asyncio
async def test_list_ports_empty():
    async with TestSessionLocal() as db:
        driver = await _create_driver(db)
        dev_tmpl = await _create_device_template(db, driver.id)
        device = await _create_device(db, dev_tmpl.id)
        ports = await list_ports(db, device.id)
        assert ports == []
