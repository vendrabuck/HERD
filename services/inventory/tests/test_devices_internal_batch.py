"""Unit tests for POST /internal/devices/batch (ADR 0014 phase 1, issue #34).

Feeds cabling's L3 routing-intent validation pass. Drives the route handler
directly against ORM-seeded rows (Device -> DeviceTemplate -> DriverPackage), the
same style test_devices_internal.py's driver-upload helper serves, but without the
admin-API round trip since only identity/type fields matter here.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base
from app.models.device import Device, DeviceStatus, TopologyType
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    yield


async def _make_device(
    *, name: str, connection_type: str | None, status=DeviceStatus.AVAILABLE
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        driver_id = None
        if connection_type is not None:
            driver = DriverPackage(
                name=f"{name}-driver",
                connection_type=connection_type,
                filename="d.zip",
                storage_key=f"key-{name}",
                size_bytes=10,
                sha256="0" * 64,
                uploaded_by="tester",
            )
            db.add(driver)
            await db.flush()
            driver_id = driver.id

        template = DeviceTemplate(name=f"{name}-tmpl", driver_id=driver_id, sections=[])
        db.add(template)
        await db.flush()

        device = Device(
            name=name,
            template_id=template.id,
            topology_type=TopologyType.PHYSICAL,
            status=status,
            field_data={},
        )
        db.add(device)
        await db.commit()
        return device.id


@pytest.mark.asyncio
async def test_batch_returns_identity_and_connection_type():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    device_id = await _make_device(name="sw1", connection_type="Layer 3 Switch")
    async with TestSessionLocal() as db:
        result = await get_devices_batch_internal(
            body=InternalDeviceBatchRequest(device_ids=[device_id]),
            db=db,
            x_internal_token=INTERNAL_TOKEN,
        )
    assert len(result) == 1
    assert result[0].id == device_id
    assert result[0].name == "sw1"
    assert result[0].connection_type == "Layer 3 Switch"
    assert result[0].status == DeviceStatus.AVAILABLE


@pytest.mark.asyncio
async def test_batch_device_with_no_driver_has_null_connection_type():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    device_id = await _make_device(name="nodriver", connection_type=None)
    async with TestSessionLocal() as db:
        result = await get_devices_batch_internal(
            body=InternalDeviceBatchRequest(device_ids=[device_id]),
            db=db,
            x_internal_token=INTERNAL_TOKEN,
        )
    assert result[0].connection_type is None


@pytest.mark.asyncio
async def test_batch_omits_ids_that_do_not_exist():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    device_id = await _make_device(name="sw2", connection_type="Layer 3 Switch")
    missing_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        result = await get_devices_batch_internal(
            body=InternalDeviceBatchRequest(device_ids=[device_id, missing_id]),
            db=db,
            x_internal_token=INTERNAL_TOKEN,
        )
    assert {r.id for r in result} == {device_id}


@pytest.mark.asyncio
async def test_batch_empty_ids_returns_empty_list_no_query():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    async with TestSessionLocal() as db:
        result = await get_devices_batch_internal(
            body=InternalDeviceBatchRequest(device_ids=[]),
            db=db,
            x_internal_token=INTERNAL_TOKEN,
        )
    assert result == []


@pytest.mark.asyncio
async def test_batch_deduplicates_repeated_ids():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    device_id = await _make_device(name="dup", connection_type="Layer 3 Switch")
    async with TestSessionLocal() as db:
        result = await get_devices_batch_internal(
            body=InternalDeviceBatchRequest(device_ids=[device_id, device_id]),
            db=db,
            x_internal_token=INTERNAL_TOKEN,
        )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_batch_wrong_token_403():
    from app.routers.devices import InternalDeviceBatchRequest, get_devices_batch_internal

    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await get_devices_batch_internal(
                body=InternalDeviceBatchRequest(device_ids=[]),
                db=db,
                x_internal_token="wrong",
            )
    assert exc.value.status_code == 403


def test_batch_request_rejects_more_than_cap_ids():
    from app.routers.devices import INTERNAL_DEVICE_BATCH_MAX_IDS, InternalDeviceBatchRequest
    from pydantic import ValidationError

    too_many = [uuid.uuid4() for _ in range(INTERNAL_DEVICE_BATCH_MAX_IDS + 1)]
    with pytest.raises(ValidationError):
        InternalDeviceBatchRequest(device_ids=too_many)


def test_batch_request_accepts_exactly_cap_ids():
    from app.routers.devices import INTERNAL_DEVICE_BATCH_MAX_IDS, InternalDeviceBatchRequest

    exactly_cap = [uuid.uuid4() for _ in range(INTERNAL_DEVICE_BATCH_MAX_IDS)]
    body = InternalDeviceBatchRequest(device_ids=exactly_cap)
    assert len(body.device_ids) == INTERNAL_DEVICE_BATCH_MAX_IDS
