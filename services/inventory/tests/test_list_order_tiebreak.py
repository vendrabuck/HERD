"""Inventory's offset-paginated lists end their order with the id (issue #1589).

`GET /devices`, `GET /templates`, and `GET /hypervisors` sort by `created_at`
descending and `GET /devices/{id}/apply-jobs` by `scheduled_for` descending. Rows
written in one transaction share `created_at` (Postgres `now()` is the
transaction's start), two jobs can be scheduled for one instant, and offset pages
over a tie have no defined order. The id, ascending, is the last sort key, so a
full skip/limit walk returns every row exactly once.

Each test makes the tie real (one explicit timestamp for a block of rows) with
one row on each side of it, and inserts the block in an order that is neither
ascending nor descending by id, so neither a forward nor a backward scan of
SQLite's table or an index yields the order under test: without the id key the
exact-order assertion fails.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device, DeviceStatus, TopologyType
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.models.driver_package import DriverPackage
from app.models.hypervisor import Hypervisor
from app.models.template import DeviceTemplate
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

TIE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
INSERT_ORDER = [4, 1, 7, 3, 6, 2, 5]
# Newest first; the tied block by id ascending; the oldest row last.
EXPECTED_NUMBERS = [100, 1, 2, 3, 4, 5, 6, 7, 50]
LIMITS = [1, 2, 3, 4, 500]


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=(0x1589 << 64) | n)


def _rows() -> list[tuple[int, datetime]]:
    """(id number, timestamp) in insertion order."""
    rows = [(100, TIE + timedelta(hours=1))]
    rows += [(n, TIE) for n in INSERT_ORDER]
    rows += [(50, TIE - timedelta(hours=1))]
    return rows


EXPECTED = [str(_id(n)) for n in EXPECTED_NUMBERS]


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _walk(client, path: str, limit: int, expected_total: int) -> list[str]:
    seen: list[str] = []
    for skip in range(0, expected_total, limit):
        resp = await client.get(path, params={"skip": skip, "limit": limit})
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == expected_total
        seen += [row["id"] for row in resp.json()["items"]]
    return seen


async def _driver(session) -> DriverPackage:
    driver = DriverPackage(
        name="TieDriver",
        connection_type="Management",
        filename="tie.zip",
        storage_key="drivers/tie.zip",
        size_bytes=10,
        sha256="tie",
        uploaded_by="admin",
    )
    session.add(driver)
    await session.flush()
    return driver


@pytest.mark.parametrize("limit", LIMITS)
async def test_templates_list_breaks_created_at_ties_by_id(client, limit):
    async with TestSessionLocal() as session:
        driver = await _driver(session)
        for n, created in _rows():
            session.add(
                DeviceTemplate(
                    id=_id(n),
                    name=f"tie-tpl-{n}",
                    driver_id=driver.id,
                    sections=[],
                    created_at=created,
                )
            )
            await session.flush()
        await session.commit()
    seen = await _walk(client, "/templates", limit, len(EXPECTED))
    assert seen == EXPECTED


@pytest.mark.parametrize("limit", LIMITS)
async def test_hypervisors_list_breaks_created_at_ties_by_id(client, limit):
    async with TestSessionLocal() as session:
        for n, created in _rows():
            session.add(
                Hypervisor(
                    id=_id(n),
                    name=f"tie-hv-{n}",
                    endpoint="https://pve.example:8006",
                    hypervisor_type="proxmox",
                    secret_id=uuid.uuid4(),
                    created_at=created,
                )
            )
            await session.flush()
        await session.commit()
    seen = await _walk(client, "/hypervisors", limit, len(EXPECTED))
    assert seen == EXPECTED


@pytest.mark.parametrize("limit", LIMITS)
async def test_devices_list_breaks_created_at_ties_by_id(client, limit):
    async with TestSessionLocal() as session:
        driver = await _driver(session)
        template = DeviceTemplate(name="tie-dev-tpl", driver_id=driver.id, sections=[])
        session.add(template)
        await session.flush()
        for n, created in _rows():
            session.add(
                Device(
                    id=_id(n),
                    name=f"tie-dev-{n}",
                    template_id=template.id,
                    topology_type=TopologyType.PHYSICAL,
                    status=DeviceStatus.AVAILABLE,
                    field_data={},
                    created_at=created,
                )
            )
            await session.flush()
        await session.commit()
    seen = await _walk(client, "/devices", limit, len(EXPECTED))
    assert seen == EXPECTED


@pytest.mark.parametrize("limit", LIMITS)
async def test_apply_jobs_list_breaks_scheduled_for_ties_by_id(client, limit):
    async with TestSessionLocal() as session:
        driver = await _driver(session)
        template = DeviceTemplate(name="tie-job-tpl", driver_id=driver.id, sections=[])
        session.add(template)
        await session.flush()
        devices = []
        for name in ("tie-job-dev", "tie-job-other"):
            device = Device(
                name=name,
                template_id=template.id,
                topology_type=TopologyType.PHYSICAL,
                status=DeviceStatus.AVAILABLE,
                field_data={},
            )
            session.add(device)
            await session.flush()
            version = DeviceConfigVersion(
                device_id=device.id,
                version_number=1,
                connection_type="Management",
                config={"vlan": 100},
                created_by=uuid.uuid4(),
            )
            session.add(version)
            await session.flush()
            devices.append((device, version))
        (device, version), (other, other_version) = devices
        for n, scheduled in _rows():
            session.add(
                DeviceConfigApplyJob(
                    id=_id(n),
                    device_id=device.id,
                    version_id=version.id,
                    scheduled_for=scheduled,
                    created_by=uuid.uuid4(),
                )
            )
            await session.flush()
        # Another device's jobs on the same instant never reach this device's list.
        for n in INSERT_ORDER:
            session.add(
                DeviceConfigApplyJob(
                    id=_id(1000 + n),
                    device_id=other.id,
                    version_id=other_version.id,
                    scheduled_for=TIE,
                    created_by=uuid.uuid4(),
                )
            )
        await session.commit()
    seen = await _walk(client, f"/devices/{device.id}/apply-jobs", limit, len(EXPECTED))
    assert seen == EXPECTED
