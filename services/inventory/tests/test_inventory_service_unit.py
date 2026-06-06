"""Unit tests for inventory_service.py - direct service layer calls."""

import uuid

import pytest
from app.database import Base
from app.models.device import DeviceStatus, TopologyType
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.device import DeviceCreate, DeviceUpdate
from app.services.inventory_service import (
    create_device,
    delete_device,
    get_device,
    get_devices_by_ids,
    list_devices,
    set_device_status,
    update_device,
    validate_field_data,
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


async def _create_driver(db) -> DriverPackage:
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


async def _create_infra_driver(db) -> DriverPackage:
    driver = DriverPackage(
        name="InfraDriver",
        connection_type="Layer 1 Switch",
        filename="infra.zip",
        storage_key="infra/infra.zip",
        size_bytes=100,
        sha256="def456",
        uploaded_by="admin",
    )
    db.add(driver)
    await db.commit()
    await db.refresh(driver)
    return driver


async def _create_template(db, driver=None, name="TestTemplate") -> DeviceTemplate:
    if driver is None:
        driver = await _create_driver(db)
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name=name,
        template_type="device",
        driver_id=driver.id,
        sections=[
            {
                "name": "General",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string", "required": True},
                    {"key": "notes", "label": "Notes", "type": "string"},
                ],
            }
        ],
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


# --- validate_field_data ---


def test_validate_field_data_unknown_field():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "a", "label": "A", "type": "string"}]}],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {"a": "ok", "unknown": "bad"})
    assert exc.value.status_code == 422
    assert "unknown" in exc.value.detail.lower()


def test_validate_field_data_required_missing():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[
            {
                "name": "S",
                "fields": [{"key": "a", "label": "A", "type": "string", "required": True}],
            }
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {})
    assert exc.value.status_code == 422
    assert "a" in exc.value.detail


def test_validate_field_data_default_applied():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[
            {
                "name": "S",
                "fields": [
                    {
                        "key": "region",
                        "label": "Region",
                        "type": "string",
                        "required": True,
                        "default": "us-west-1",
                    }
                ],
            }
        ],
    )
    field_data = {}
    validate_field_data(template, field_data)
    assert field_data["region"] == "us-west-1"


def test_validate_field_data_string_type_check():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "a", "label": "A", "type": "string"}]}],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {"a": 123})
    assert exc.value.status_code == 422


def test_validate_field_data_number_type_check():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "a", "label": "A", "type": "number"}]}],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {"a": "not-a-number"})
    assert exc.value.status_code == 422


def test_validate_field_data_boolean_type_check():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "a", "label": "A", "type": "boolean"}]}],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {"a": "not-a-bool"})
    assert exc.value.status_code == 422


def test_validate_field_data_dropdown_invalid_value():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[
            {
                "name": "S",
                "fields": [{"key": "a", "label": "A", "type": "dropdown", "options": ["x", "y"]}],
            }
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_field_data(template, {"a": "z"})
    assert exc.value.status_code == 422


def test_validate_field_data_password_type():
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "pw", "label": "PW", "type": "password"}]}],
    )
    # String should pass
    validate_field_data(template, {"pw": "secret123"})
    # Non-string should fail
    with pytest.raises(HTTPException):
        validate_field_data(template, {"pw": 12345})


def test_validate_field_data_empty_value_skipped():
    """Empty string for non-required field is skipped (no type check)."""
    template = DeviceTemplate(
        vendor="V",
        model="M",
        name="T",
        sections=[{"name": "S", "fields": [{"key": "a", "label": "A", "type": "number"}]}],
    )
    validate_field_data(template, {"a": ""})  # should not raise


# --- list_devices ---


@pytest.mark.asyncio
async def test_list_devices_empty():
    async with TestSessionLocal() as db:
        devices, total = await list_devices(db)
        assert devices == []
        assert total == 0


@pytest.mark.asyncio
async def test_list_devices_with_filters():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="FW-01",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        await create_device(db, data)
        data2 = DeviceCreate(
            name="FW-02",
            template_id=template.id,
            topology_type=TopologyType.CLOUD,
            field_data={"model": "EX4400"},
        )
        await create_device(db, data2)

        # Filter by topology_type
        devices, total = await list_devices(db, topology_type=TopologyType.PHYSICAL)
        assert total == 1
        assert devices[0].name == "FW-01"

        # Filter by template_id
        devices, total = await list_devices(db, template_id=template.id)
        assert total == 2

        # Search
        devices, total = await list_devices(db, search="FW-01")
        assert total == 1


@pytest.mark.asyncio
async def test_list_devices_visible_device_ids_filter():
    """visible_device_ids restricts results to only those device IDs."""
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data1 = DeviceCreate(
            name="Vis-01",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        dev1 = await create_device(db, data1)
        data2 = DeviceCreate(
            name="Vis-02",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX4400"},
        )
        await create_device(db, data2)

        # Only the first device should be visible
        devices, total = await list_devices(db, visible_device_ids={dev1.id})
        assert total == 1
        assert devices[0].id == dev1.id


@pytest.mark.asyncio
async def test_list_devices_dut_only():
    """dut_only=True filters to only Management connection_type devices."""
    async with TestSessionLocal() as db:
        dut_driver = await _create_driver(db)
        infra_driver = await _create_infra_driver(db)

        dut_template = await _create_template(db, driver=dut_driver, name="DUT Tmpl")
        infra_template = DeviceTemplate(
            vendor="V",
            model="M",
            name="Infra Tmpl",
            template_type="device",
            driver_id=infra_driver.id,
            sections=[{"name": "S", "fields": [{"key": "model", "label": "M", "type": "string"}]}],
        )
        db.add(infra_template)
        await db.commit()
        await db.refresh(infra_template)

        d1 = DeviceCreate(
            name="DUT-1",
            template_id=dut_template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        await create_device(db, d1)
        d2 = DeviceCreate(
            name="INFRA-1",
            template_id=infra_template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "Y"},
        )
        await create_device(db, d2)

        # dut_only=True: only DUT
        devices, total = await list_devices(db, dut_only=True)
        assert total == 1
        assert devices[0].name == "DUT-1"

        # dut_only=False: both
        devices, total = await list_devices(db, dut_only=False)
        assert total == 2


@pytest.mark.asyncio
async def test_list_devices_pagination():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        for i in range(5):
            d = DeviceCreate(
                name=f"Dev-{i}",
                template_id=template.id,
                topology_type=TopologyType.PHYSICAL,
                field_data={"model": f"M{i}"},
            )
            await create_device(db, d)

        devices, total = await list_devices(db, skip=2, limit=2)
        assert total == 5
        assert len(devices) == 2


# --- get_device ---


@pytest.mark.asyncio
async def test_get_device_found():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="FW-01",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        created = await create_device(db, data)
        device = await get_device(db, created.id)
        assert device is not None
        assert device.name == "FW-01"


@pytest.mark.asyncio
async def test_get_device_not_found():
    async with TestSessionLocal() as db:
        device = await get_device(db, uuid.uuid4())
        assert device is None


# --- get_devices_by_ids ---


@pytest.mark.asyncio
async def test_get_devices_by_ids():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        d1 = await create_device(
            db,
            DeviceCreate(
                name="D1",
                template_id=template.id,
                topology_type=TopologyType.PHYSICAL,
                field_data={"model": "M1"},
            ),
        )
        d2 = await create_device(
            db,
            DeviceCreate(
                name="D2",
                template_id=template.id,
                topology_type=TopologyType.PHYSICAL,
                field_data={"model": "M2"},
            ),
        )
        devices = await get_devices_by_ids(db, [d1.id, d2.id])
        assert len(devices) == 2


@pytest.mark.asyncio
async def test_get_devices_by_ids_empty():
    async with TestSessionLocal() as db:
        devices = await get_devices_by_ids(db, [])
        assert devices == []


# --- create_device ---


@pytest.mark.asyncio
async def test_create_device_success():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="NewDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        device = await create_device(db, data)
        assert device.name == "NewDev"
        assert device.template_id == template.id


@pytest.mark.asyncio
async def test_create_device_template_not_found():
    async with TestSessionLocal() as db:
        data = DeviceCreate(
            name="BadDev",
            template_id=uuid.uuid4(),
            topology_type=TopologyType.PHYSICAL,
            field_data={},
        )
        with pytest.raises(HTTPException) as exc:
            await create_device(db, data)
        assert exc.value.status_code == 422
        assert "Template not found" in exc.value.detail


@pytest.mark.asyncio
async def test_create_device_port_template_rejected():
    async with TestSessionLocal() as db:
        template = DeviceTemplate(
            vendor="V",
            model="M",
            name="PortTmpl",
            template_type="port",
            sections=[{"name": "S", "fields": []}],
        )
        db.add(template)
        await db.commit()
        await db.refresh(template)

        data = DeviceCreate(
            name="BadDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={},
        )
        with pytest.raises(HTTPException) as exc:
            await create_device(db, data)
        assert exc.value.status_code == 422
        assert "not a device template" in exc.value.detail


@pytest.mark.asyncio
async def test_create_device_duplicate_name():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="Dupe",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "M"},
        )
        await create_device(db, data)
        with pytest.raises(HTTPException) as exc:
            await create_device(db, data)
        assert exc.value.status_code == 409


# --- update_device ---


@pytest.mark.asyncio
async def test_update_device_success():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="UpdDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        device = await create_device(db, data)
        updated = await update_device(
            db, device.id, DeviceUpdate(name="Renamed"), modified_by=uuid.uuid4()
        )
        assert updated is not None
        assert updated.name == "Renamed"


@pytest.mark.asyncio
async def test_update_device_not_found():
    async with TestSessionLocal() as db:
        result = await update_device(db, uuid.uuid4(), DeviceUpdate(name="Ghost"))
        assert result is None


@pytest.mark.asyncio
async def test_update_device_field_data_validated():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="ValDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        device = await create_device(db, data)
        with pytest.raises(HTTPException) as exc:
            await update_device(
                db, device.id, DeviceUpdate(field_data={"model": "OK", "unknown": "bad"})
            )
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_device_duplicate_name():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        d1 = DeviceCreate(
            name="Dev1",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "M1"},
        )
        await create_device(db, d1)
        d2 = DeviceCreate(
            name="Dev2",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "M2"},
        )
        dev2 = await create_device(db, d2)
        with pytest.raises(HTTPException) as exc:
            await update_device(db, dev2.id, DeviceUpdate(name="Dev1"))
        assert exc.value.status_code == 409


# --- delete_device ---


@pytest.mark.asyncio
async def test_delete_device_success():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="DelDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        device = await create_device(db, data)
        result = await delete_device(db, device.id)
        assert result is True


@pytest.mark.asyncio
async def test_delete_device_not_found():
    async with TestSessionLocal() as db:
        result = await delete_device(db, uuid.uuid4())
        assert result is False


# --- set_device_status ---


@pytest.mark.asyncio
async def test_set_device_status_success():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="StatusDev",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "EX3300"},
        )
        device = await create_device(db, data)
        updated = await set_device_status(db, device.id, DeviceStatus.RESERVED)
        assert updated is not None
        assert updated.status == DeviceStatus.RESERVED


@pytest.mark.asyncio
async def test_set_device_status_not_found():
    async with TestSessionLocal() as db:
        result = await set_device_status(db, uuid.uuid4(), DeviceStatus.AVAILABLE)
        assert result is None
