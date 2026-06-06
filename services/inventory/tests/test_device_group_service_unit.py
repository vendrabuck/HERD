"""Unit tests for device_group_service.py - direct service layer calls."""

import uuid
from unittest.mock import patch

import pytest
from app.database import Base
from app.models.device import Device, TopologyType
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.services.device_group_service import (
    add_device_to_no_pool,
    bulk_add_devices,
    bulk_add_user_groups,
    bulk_remove_devices,
    bulk_remove_user_groups,
    create_device_group,
    delete_device_group,
    get_device_group,
    get_no_pool_group,
    get_visible_device_ids,
    list_device_groups,
    update_device_group,
)
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
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


async def _make_device(db, name="Dev1") -> Device:
    """Create a minimal device for testing (driver, template, device)."""
    driver = DriverPackage(
        name=f"Driver-{name}",
        connection_type="Management",
        filename="test.zip",
        storage_key=f"test/{name}.zip",
        size_bytes=100,
        sha256="abc123",
        uploaded_by="admin",
    )
    db.add(driver)
    await db.commit()
    await db.refresh(driver)

    template = DeviceTemplate(
        name=f"Tmpl-{name}",
        template_type="device",
        driver_id=driver.id,
        sections=[{"name": "S", "fields": []}],
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    device = Device(
        name=name,
        template_id=template.id,
        topology_type=TopologyType.PHYSICAL,
        field_data={},
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


# --- create_device_group ---


@pytest.mark.asyncio
async def test_create_device_group_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", "Test lab", uuid.uuid4())
        assert group.name == "Lab1"
        assert group.description == "Test lab"


@pytest.mark.asyncio
async def test_create_device_group_duplicate():
    async with TestSessionLocal() as db:
        await create_device_group(db, "Lab1", "desc")
        with pytest.raises(HTTPException) as exc:
            await create_device_group(db, "Lab1", "desc2")
        assert exc.value.status_code == 409


# --- list_device_groups ---


@pytest.mark.asyncio
async def test_list_device_groups_empty():
    async with TestSessionLocal() as db:
        groups, total = await list_device_groups(db)
        assert groups == []
        assert total == 0


@pytest.mark.asyncio
async def test_list_device_groups_pagination():
    async with TestSessionLocal() as db:
        for i in range(5):
            await create_device_group(db, f"Group{i}", None)
        groups, total = await list_device_groups(db, skip=1, limit=2)
        assert total == 5
        assert len(groups) == 2


# --- get_device_group ---


@pytest.mark.asyncio
async def test_get_device_group_found():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", "desc")
        fetched = await get_device_group(db, group.id)
        assert fetched is not None
        assert fetched.name == "Lab1"


@pytest.mark.asyncio
async def test_get_device_group_not_found():
    async with TestSessionLocal() as db:
        fetched = await get_device_group(db, uuid.uuid4())
        assert fetched is None


# --- update_device_group ---


@pytest.mark.asyncio
async def test_update_device_group_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", "desc")
        updated = await update_device_group(
            db, group, "Lab1-Updated", "new desc", modified_by=uuid.uuid4()
        )
        assert updated.name == "Lab1-Updated"
        assert updated.description == "new desc"


@pytest.mark.asyncio
async def test_update_device_group_duplicate_name():
    async with TestSessionLocal() as db:
        await create_device_group(db, "Lab1", "desc")
        group2 = await create_device_group(db, "Lab2", "desc")
        with pytest.raises(HTTPException) as exc:
            await update_device_group(db, group2, "Lab1", None)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_update_device_group_partial():
    """Update only name or only description (partial update)."""
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", "orig")
        # Update only name
        updated = await update_device_group(db, group, "Lab1-New", None)
        assert updated.name == "Lab1-New"
        assert updated.description == "orig"
        # Update only description
        updated = await update_device_group(db, updated, None, "new-desc")
        assert updated.name == "Lab1-New"
        assert updated.description == "new-desc"


# --- delete_device_group ---


@pytest.mark.asyncio
async def test_delete_device_group_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", "desc")
        await delete_device_group(db, group)
        fetched = await get_device_group(db, group.id)
        assert fetched is None


# --- bulk_add_devices ---


@pytest.mark.asyncio
async def test_bulk_add_devices_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        dev2 = await _make_device(db, "Dev2")
        added, skipped = await bulk_add_devices(db, group.id, [dev1.id, dev2.id])
        assert added == 2
        assert skipped == 0


@pytest.mark.asyncio
async def test_bulk_add_devices_skip_duplicates():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        await bulk_add_devices(db, group.id, [dev1.id])
        added, skipped = await bulk_add_devices(db, group.id, [dev1.id])
        assert added == 0
        assert skipped == 1


@pytest.mark.asyncio
async def test_bulk_add_devices_removes_from_no_pool():
    """Adding devices to another group removes them from 'No Pool'."""
    async with TestSessionLocal() as db:
        no_pool = await create_device_group(db, "No Pool", "Default")
        other_group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")

        # Add to No Pool
        await bulk_add_devices(db, no_pool.id, [dev1.id])

        # Add to another group: should auto-remove from No Pool
        await bulk_add_devices(db, other_group.id, [dev1.id])

        # Verify removed from No Pool
        reloaded = await get_device_group(db, no_pool.id)
        device_ids = [d.device_id for d in reloaded.devices]
        assert dev1.id not in device_ids


async def _commit_raising_integrity_error_once(db):
    """Return a side_effect coroutine that raises IntegrityError on first commit.

    Simulates a concurrent duplicate insert tripping the unique constraint at
    commit time. Subsequent commits (there are none in these paths) fall through
    to the real commit so the session stays usable.
    """
    real_commit = db.commit
    state = {"fired": False}

    async def side_effect():
        if not state["fired"]:
            state["fired"] = True
            raise IntegrityError("INSERT ...", {}, Exception("UNIQUE constraint failed"))
        await real_commit()

    return side_effect


@pytest.mark.asyncio
async def test_bulk_add_devices_concurrent_duplicate_skips_gracefully():
    """A unique-constraint trip on commit must be absorbed, not raised as 500.

    Simulates the TOCTOU race where a concurrent writer inserts the same
    (group, device) row between our existence check and our commit. The fix
    catches IntegrityError, rolls back, and recounts actually-present rows so
    the call returns a graceful (added, skipped) tuple instead of letting the
    IntegrityError surface as an unhandled 500.
    """
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        group_id = group.id
        device_id = dev1.id

        side_effect = await _commit_raising_integrity_error_once(db)
        with patch.object(db, "commit", side_effect=side_effect):
            # Must NOT raise IntegrityError; must return a clean tuple.
            added, skipped = await bulk_add_devices(db, group_id, [device_id])

    # The conflicting commit was rolled back; recount sees nothing committed, so
    # the device is reported skipped rather than crashing the request.
    assert added + skipped == 1
    assert skipped == 1
    assert added == 0


@pytest.mark.asyncio
async def test_bulk_add_user_groups_concurrent_duplicate_skips_gracefully():
    """Same TOCTOU race as devices, for the user-group permission path.

    A unique-constraint trip on commit must be caught, rolled back, and
    recounted so the call returns gracefully rather than raising IntegrityError
    (500).
    """
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        group_id = group.id
        ug_id = uuid.uuid4()

        side_effect = await _commit_raising_integrity_error_once(db)
        with patch.object(db, "commit", side_effect=side_effect):
            added, skipped = await bulk_add_user_groups(db, group_id, [ug_id])

    assert added + skipped == 1
    assert skipped == 1
    assert added == 0


# --- bulk_remove_devices ---


@pytest.mark.asyncio
async def test_bulk_remove_devices_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        await bulk_add_devices(db, group.id, [dev1.id])
        removed, not_found = await bulk_remove_devices(db, group.id, [dev1.id])
        assert removed == 1
        assert not_found == 0


@pytest.mark.asyncio
async def test_bulk_remove_devices_not_found():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        removed, not_found = await bulk_remove_devices(db, group.id, [uuid.uuid4()])
        assert removed == 0
        assert not_found == 1


# --- bulk_add_user_groups ---


@pytest.mark.asyncio
async def test_bulk_add_user_groups_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        ug1 = uuid.uuid4()
        ug2 = uuid.uuid4()
        added, skipped = await bulk_add_user_groups(
            db, group.id, [ug1, ug2], assigned_by=uuid.uuid4()
        )
        assert added == 2
        assert skipped == 0


@pytest.mark.asyncio
async def test_bulk_add_user_groups_skip_duplicates():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        ug1 = uuid.uuid4()
        await bulk_add_user_groups(db, group.id, [ug1])
        added, skipped = await bulk_add_user_groups(db, group.id, [ug1])
        assert added == 0
        assert skipped == 1


# --- bulk_remove_user_groups ---


@pytest.mark.asyncio
async def test_bulk_remove_user_groups_success():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        ug1 = uuid.uuid4()
        await bulk_add_user_groups(db, group.id, [ug1])
        removed, not_found = await bulk_remove_user_groups(db, group.id, [ug1])
        assert removed == 1
        assert not_found == 0


@pytest.mark.asyncio
async def test_bulk_remove_user_groups_not_found():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        removed, not_found = await bulk_remove_user_groups(db, group.id, [uuid.uuid4()])
        assert removed == 0
        assert not_found == 1


# --- get_visible_device_ids ---


@pytest.mark.asyncio
async def test_get_visible_device_ids_empty():
    async with TestSessionLocal() as db:
        visible = await get_visible_device_ids(db, [])
        assert visible == set()


@pytest.mark.asyncio
async def test_get_visible_device_ids_with_permissions():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        dev2 = await _make_device(db, "Dev2")
        await bulk_add_devices(db, group.id, [dev1.id, dev2.id])
        ug_id = uuid.uuid4()
        await bulk_add_user_groups(db, group.id, [ug_id])

        visible = await get_visible_device_ids(db, [ug_id])
        assert dev1.id in visible
        assert dev2.id in visible


@pytest.mark.asyncio
async def test_get_visible_device_ids_no_permissions():
    async with TestSessionLocal() as db:
        group = await create_device_group(db, "Lab1", None)
        dev1 = await _make_device(db, "Dev1")
        await bulk_add_devices(db, group.id, [dev1.id])
        # No permissions added
        visible = await get_visible_device_ids(db, [uuid.uuid4()])
        assert visible == set()


# --- get_no_pool_group ---


@pytest.mark.asyncio
async def test_get_no_pool_group_exists():
    async with TestSessionLocal() as db:
        await create_device_group(db, "No Pool", "Default")
        no_pool = await get_no_pool_group(db)
        assert no_pool is not None
        assert no_pool.name == "No Pool"


@pytest.mark.asyncio
async def test_get_no_pool_group_not_exists():
    async with TestSessionLocal() as db:
        no_pool = await get_no_pool_group(db)
        assert no_pool is None


# --- add_device_to_no_pool ---


@pytest.mark.asyncio
async def test_add_device_to_no_pool_success():
    async with TestSessionLocal() as db:
        no_pool = await create_device_group(db, "No Pool", "Default")
        dev = await _make_device(db, "NPDev")
        await add_device_to_no_pool(db, dev.id)

        reloaded = await get_device_group(db, no_pool.id)
        device_ids = [d.device_id for d in reloaded.devices]
        assert dev.id in device_ids


@pytest.mark.asyncio
async def test_add_device_to_no_pool_no_group():
    """If No Pool group does not exist, add_device_to_no_pool is a no-op."""
    async with TestSessionLocal() as db:
        dev = await _make_device(db, "NPDev")
        # Should not raise
        await add_device_to_no_pool(db, dev.id)


@pytest.mark.asyncio
async def test_add_device_to_no_pool_duplicate():
    """Adding same device twice to No Pool should not raise (IntegrityError caught)."""
    async with TestSessionLocal() as db:
        await create_device_group(db, "No Pool", "Default")
        dev = await _make_device(db, "NPDev")
        await add_device_to_no_pool(db, dev.id)
        # Second call should be a silent no-op
        await add_device_to_no_pool(db, dev.id)
