"""DB constraint coverage: uniqueness, FK cascade, and enum validation
at the ORM layer for the inventory schema.

Foreign keys are enforced on SQLite only when PRAGMA foreign_keys=ON.
We enable it explicitly so ondelete=CASCADE and FK violations exercise the
same code paths as production Postgres.
"""

import uuid

import pytest
from app.database import Base
from app.models.device import Device, DeviceStatus
from app.models.device_group import DeviceGroup, DeviceGroupDevice
from app.models.template import DeviceTemplate
from herd_common.enums import TopologyType
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _make_template(session, name="T1") -> DeviceTemplate:
    tpl = DeviceTemplate(name=name, sections=[], vendor="TestVendor", model="TestModel")
    session.add(tpl)
    await session.flush()
    return tpl


@pytest.mark.asyncio
async def test_device_name_is_unique():
    async with SessionLocal() as session:
        tpl = await _make_template(session)
        session.add(Device(name="shared", template_id=tpl.id, topology_type=TopologyType.PHYSICAL))
        await session.commit()

    async with SessionLocal() as session:
        tpl = (await session.execute(select(DeviceTemplate))).scalar_one()
        session.add(Device(name="shared", template_id=tpl.id, topology_type=TopologyType.PHYSICAL))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_template_name_is_unique():
    async with SessionLocal() as session:
        session.add(DeviceTemplate(name="dup", sections=[], vendor="V", model="M"))
        await session.commit()

    async with SessionLocal() as session:
        session.add(DeviceTemplate(name="dup", sections=[], vendor="V", model="M"))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_device_group_name_is_unique():
    async with SessionLocal() as session:
        session.add(DeviceGroup(name="Group A", description="first"))
        await session.commit()

    async with SessionLocal() as session:
        session.add(DeviceGroup(name="Group A", description="second"))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_device_group_device_pair_is_unique():
    async with SessionLocal() as session:
        tpl = await _make_template(session)
        dev = Device(name="d1", template_id=tpl.id, topology_type=TopologyType.PHYSICAL)
        grp = DeviceGroup(name="G1")
        session.add_all([dev, grp])
        await session.flush()
        session.add(DeviceGroupDevice(device_group_id=grp.id, device_id=dev.id))
        await session.commit()

        session.add(DeviceGroupDevice(device_group_id=grp.id, device_id=dev.id))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_device_group_device_rejects_missing_device_fk():
    async with SessionLocal() as session:
        grp = DeviceGroup(name="Gfk")
        session.add(grp)
        await session.flush()
        session.add(DeviceGroupDevice(device_group_id=grp.id, device_id=uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_delete_device_cascades_device_group_device():
    async with SessionLocal() as session:
        tpl = await _make_template(session)
        dev = Device(name="dcascade", template_id=tpl.id, topology_type=TopologyType.PHYSICAL)
        grp = DeviceGroup(name="Gcas")
        session.add_all([dev, grp])
        await session.flush()
        session.add(DeviceGroupDevice(device_group_id=grp.id, device_id=dev.id))
        await session.commit()

        device_id = dev.id
        await session.delete(dev)
        await session.commit()

    async with SessionLocal() as session:
        remaining = (
            await session.execute(
                select(DeviceGroupDevice).where(DeviceGroupDevice.device_id == device_id)
            )
        ).all()
        assert remaining == []


@pytest.mark.asyncio
async def test_device_roundtrips_enum_values():
    """Verify enum values round-trip through the ORM. On Postgres the native
    enum types additionally reject out-of-range values at the DB layer; SQLite
    does not enforce this so that case lives in integration tests, not here."""
    async with SessionLocal() as session:
        tpl = await _make_template(session)
        dev = Device(
            name="rsrv",
            template_id=tpl.id,
            topology_type=TopologyType.PHYSICAL,
            status=DeviceStatus.RESERVED,
        )
        session.add(dev)
        await session.commit()

    async with SessionLocal() as session:
        reloaded = (await session.execute(select(Device).where(Device.name == "rsrv"))).scalar_one()
        assert reloaded.status == DeviceStatus.RESERVED
        assert reloaded.topology_type == TopologyType.PHYSICAL


@pytest.mark.asyncio
async def test_device_defaults_to_available_status():
    async with SessionLocal() as session:
        tpl = await _make_template(session)
        dev = Device(name="defstatus", template_id=tpl.id, topology_type=TopologyType.PHYSICAL)
        session.add(dev)
        await session.commit()
        await session.refresh(dev)
        assert dev.status == DeviceStatus.AVAILABLE


# --- issue #1019: group bulk add with an unknown device id, foreign keys ON ---


@pytest.mark.asyncio
async def test_group_bulk_add_mixed_unknown_id_is_422_and_adds_nothing_with_fk_on():
    """issue #1019 at the production constraint level: with the foreign key
    enforced, one unknown id among real ones used to fail at an autoflush (500)
    and drop the valid rows. Now every id is resolved first: 422 naming the
    unknown id, nothing added, and the session is still usable."""
    from app.services.device_group_service import bulk_add_devices
    from fastapi import HTTPException

    async with SessionLocal() as session:
        tpl = await _make_template(session)
        dev = Device(name="real", template_id=tpl.id, topology_type=TopologyType.PHYSICAL)
        group = DeviceGroup(name="G")
        session.add_all([dev, group])
        await session.commit()
        ghost = uuid.UUID("00000000-0000-0000-0000-00000000dead")

        with pytest.raises(HTTPException) as ei:
            await bulk_add_devices(session, group.id, [dev.id, ghost])
        assert ei.value.status_code == 422
        assert ei.value.detail == f"Devices not found: {ghost}"

        rows = (await session.execute(select(DeviceGroupDevice))).scalars().all()
        assert rows == []
        # The valid id on its own still adds cleanly afterwards.
        assert await bulk_add_devices(session, group.id, [dev.id]) == (1, 0)
