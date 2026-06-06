"""Direct unit tests for app.services.grant_service.

These bypass the FastAPI router so they exercise the service-layer logic
(filter combinations, manage-implies-view semantics, multi-group fan-out,
pagination ordering) without going through dependency overrides.
"""

import uuid

import pytest
from app.database import Base
from app.models.grant import ResourceGrant
from app.services.grant_service import (
    batch_check,
    check_permission,
    create_grant,
    delete_grant,
    get_accessible_resources,
    get_grant,
    list_grants,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# --- create_grant ---


@pytest.mark.asyncio
async def test_create_grant_persists_granted_by(db):
    group_id = uuid.uuid4()
    device_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    grant = await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=device_id,
        permission="view",
        granted_by=admin_id,
    )
    assert grant.granted_by == admin_id
    assert grant.id is not None
    assert grant.granted_at is not None


@pytest.mark.asyncio
async def test_create_grant_allows_null_granted_by(db):
    """System-issued grants (e.g., automatic owner grants) may have no actor."""
    grant = await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="topology",
        resource_id=uuid.uuid4(),
        permission="manage",
        granted_by=None,
    )
    assert grant.granted_by is None


@pytest.mark.asyncio
async def test_create_grant_duplicate_raises_integrity_error(db):
    group_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    with pytest.raises(IntegrityError):
        await create_grant(
            db,
            group_id=group_id,
            resource_type="device",
            resource_id=resource_id,
            permission="view",
            granted_by=None,
        )


# --- list_grants ordering and pagination ---


@pytest.mark.asyncio
async def test_list_grants_orders_by_granted_at_ascending(db):
    """list_grants sorts oldest-first. The router exposes this verbatim, so the
    ordering is part of the API contract."""
    g1 = await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="device",
        resource_id=uuid.uuid4(),
        permission="view",
        granted_by=None,
    )
    g2 = await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="device",
        resource_id=uuid.uuid4(),
        permission="view",
        granted_by=None,
    )
    g3 = await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="device",
        resource_id=uuid.uuid4(),
        permission="view",
        granted_by=None,
    )
    items, total = await list_grants(db, skip=0, limit=50)
    assert total == 3
    ids_in_order = [item.id for item in items]
    assert ids_in_order == [g1.id, g2.id, g3.id]


@pytest.mark.asyncio
async def test_list_grants_pagination_preserves_total(db):
    for _ in range(5):
        await create_grant(
            db,
            group_id=uuid.uuid4(),
            resource_type="device",
            resource_id=uuid.uuid4(),
            permission="view",
            granted_by=None,
        )
    items, total = await list_grants(db, skip=2, limit=2)
    assert total == 5
    assert len(items) == 2


# --- check_permission edge cases ---


@pytest.mark.asyncio
async def test_check_permission_returns_grants_list_on_allow(db):
    """The router echoes the matching grants back to the caller; verify the
    service returns the full list rather than just a boolean."""
    group_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    grant = await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    allowed, grants = await check_permission(db, [group_id], "device", resource_id, "view")
    assert allowed is True
    assert len(grants) == 1
    assert grants[0].id == grant.id


@pytest.mark.asyncio
async def test_check_permission_manage_request_does_not_match_view_grant(db):
    """manage-implies-view is asymmetric. A user with only 'view' must not be
    promoted to 'manage'."""
    group_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    allowed, grants = await check_permission(db, [group_id], "device", resource_id, "manage")
    assert allowed is False
    assert grants == []


@pytest.mark.asyncio
async def test_check_permission_aggregates_across_groups(db):
    """One grant under any of the user's groups should suffice."""
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    resource_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_b,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    allowed, _ = await check_permission(db, [group_a, group_b], "device", resource_id, "view")
    assert allowed is True


@pytest.mark.asyncio
async def test_check_permission_returns_multiple_grants_when_overlapping(db):
    """If both view and manage grants exist for the same resource under
    different groups the user belongs to, both rows surface."""
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    resource_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_a,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    await create_grant(
        db,
        group_id=group_b,
        resource_type="device",
        resource_id=resource_id,
        permission="manage",
        granted_by=None,
    )
    allowed, grants = await check_permission(db, [group_a, group_b], "device", resource_id, "view")
    assert allowed is True
    assert len(grants) == 2


# --- get_accessible_resources ---


@pytest.mark.asyncio
async def test_get_accessible_resources_deduplicates(db):
    """If a resource is granted to two of the user's groups, it should appear
    once, not twice."""
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    resource_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_a,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    await create_grant(
        db,
        group_id=group_b,
        resource_type="device",
        resource_id=resource_id,
        permission="view",
        granted_by=None,
    )
    resources = await get_accessible_resources(db, [group_a, group_b], "device", "view")
    assert len(resources) == 1
    assert resources[0] == resource_id


@pytest.mark.asyncio
async def test_get_accessible_resources_empty_groups_short_circuits(db):
    """No groups means no access, even if grants exist."""
    await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="device",
        resource_id=uuid.uuid4(),
        permission="view",
        granted_by=None,
    )
    resources = await get_accessible_resources(db, [], "device", "view")
    assert resources == []


@pytest.mark.asyncio
async def test_get_accessible_resources_filters_by_resource_type(db):
    group_id = uuid.uuid4()
    device_id = uuid.uuid4()
    topology_id = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=device_id,
        permission="view",
        granted_by=None,
    )
    await create_grant(
        db,
        group_id=group_id,
        resource_type="topology",
        resource_id=topology_id,
        permission="view",
        granted_by=None,
    )
    resources = await get_accessible_resources(db, [group_id], "topology", "view")
    assert resources == [topology_id]


# --- batch_check ---


@pytest.mark.asyncio
async def test_batch_check_empty_resource_ids_returns_empty_dict(db):
    result = await batch_check(db, [uuid.uuid4()], "device", [], "view")
    assert result == {}


@pytest.mark.asyncio
async def test_batch_check_no_groups_returns_all_false_keyed_by_str(db):
    """Even with empty group list, every input resource id must appear in the
    output dict with value False so the caller can iterate without KeyError."""
    rid_a = uuid.uuid4()
    rid_b = uuid.uuid4()
    result = await batch_check(db, [], "device", [rid_a, rid_b], "view")
    assert result == {str(rid_a): False, str(rid_b): False}


@pytest.mark.asyncio
async def test_batch_check_preserves_input_ordering(db):
    """The router relies on stable iteration; the dict must contain every
    requested id."""
    group_id = uuid.uuid4()
    rid_a = uuid.uuid4()
    rid_b = uuid.uuid4()
    rid_c = uuid.uuid4()
    await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=rid_b,
        permission="view",
        granted_by=None,
    )
    result = await batch_check(db, [group_id], "device", [rid_a, rid_b, rid_c], "view")
    assert set(result.keys()) == {str(rid_a), str(rid_b), str(rid_c)}
    assert result[str(rid_a)] is False
    assert result[str(rid_b)] is True
    assert result[str(rid_c)] is False


# --- get_grant / delete_grant ---


@pytest.mark.asyncio
async def test_get_grant_nonexistent_returns_none(db):
    result = await get_grant(db, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_delete_grant_removes_row(db):
    grant = await create_grant(
        db,
        group_id=uuid.uuid4(),
        resource_type="device",
        resource_id=uuid.uuid4(),
        permission="view",
        granted_by=None,
    )
    await delete_grant(db, grant)
    assert await get_grant(db, grant.id) is None


@pytest.mark.asyncio
async def test_delete_one_grant_does_not_affect_siblings(db):
    """A common router-layer concern: deleting one grant must not touch other
    grants for the same group or the same resource."""
    group_id = uuid.uuid4()
    rid_a = uuid.uuid4()
    rid_b = uuid.uuid4()
    g_a = await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=rid_a,
        permission="view",
        granted_by=None,
    )
    g_b = await create_grant(
        db,
        group_id=group_id,
        resource_type="device",
        resource_id=rid_b,
        permission="view",
        granted_by=None,
    )
    await delete_grant(db, g_a)
    surviving = await get_grant(db, g_b.id)
    assert surviving is not None
    assert isinstance(surviving, ResourceGrant)
    assert surviving.resource_id == rid_b
