"""Unit tests for device audit fields (created_by, modified_by, and denormalized names)."""

import uuid

import pytest
from app.database import Base
from app.models.device import TopologyType
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.device import DeviceCreate, DeviceResponse, DeviceUpdate
from app.services.inventory_service import create_device, update_device
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
        name="AuditDriver",
        connection_type="Management",
        filename="audit.zip",
        storage_key="audit/audit.zip",
        size_bytes=100,
        sha256="audit123",
        uploaded_by="admin",
    )
    db.add(driver)
    await db.commit()
    await db.refresh(driver)
    return driver


async def _create_template(db) -> DeviceTemplate:
    driver = await _create_driver(db)
    template = DeviceTemplate(
        name="AuditTemplate",
        template_type="device",
        driver_id=driver.id,
        vendor="V",
        model="M",
        sections=[
            {
                "name": "General",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string"},
                ],
            }
        ],
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def test_create_device_records_created_by_and_name():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        creator_id = uuid.uuid4()
        data = DeviceCreate(
            name="audit-dev-1",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        device = await create_device(db, data, created_by=creator_id, created_by_name="alice")
        assert device.created_by == creator_id
        assert device.created_by_name == "alice"
        assert device.modified_by is None
        assert device.modified_by_name is None


async def test_create_device_without_audit_fields_is_null():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="audit-dev-null",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        device = await create_device(db, data)
        assert device.created_by is None
        assert device.created_by_name is None


async def test_update_device_records_modified_by_and_name():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        creator_id = uuid.uuid4()
        modifier_id = uuid.uuid4()
        data = DeviceCreate(
            name="audit-dev-2",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        device = await create_device(db, data, created_by=creator_id, created_by_name="alice")

        updated = await update_device(
            db,
            device.id,
            DeviceUpdate(name="audit-dev-2-renamed"),
            modified_by=modifier_id,
            modified_by_name="bob",
        )
        assert updated is not None
        assert updated.modified_by == modifier_id
        assert updated.modified_by_name == "bob"
        # Creator fields unchanged.
        assert updated.created_by == creator_id
        assert updated.created_by_name == "alice"


async def test_update_device_without_modifier_leaves_modified_fields_untouched():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        data = DeviceCreate(
            name="audit-dev-3",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        device = await create_device(db, data, created_by=uuid.uuid4(), created_by_name="alice")
        # Update without passing modifier args: stays None.
        updated = await update_device(db, device.id, DeviceUpdate(name="audit-dev-3-renamed"))
        assert updated is not None
        assert updated.modified_by is None
        assert updated.modified_by_name is None


async def test_device_response_exposes_all_audit_fields():
    async with TestSessionLocal() as db:
        template = await _create_template(db)
        creator_id = uuid.uuid4()
        modifier_id = uuid.uuid4()
        data = DeviceCreate(
            name="audit-dev-4",
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            field_data={"model": "X"},
        )
        device = await create_device(db, data, created_by=creator_id, created_by_name="alice")
        await update_device(
            db,
            device.id,
            DeviceUpdate(name="audit-dev-4-renamed"),
            modified_by=modifier_id,
            modified_by_name="bob",
        )
        await db.refresh(device)

        response = DeviceResponse.model_validate(device)
        assert response.created_by == creator_id
        assert response.created_by_name == "alice"
        assert response.modified_by == modifier_id
        assert response.modified_by_name == "bob"
