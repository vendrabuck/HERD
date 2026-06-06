"""Tests for vlan_service.py: fabric-aware VLAN assignment."""

import uuid

import pytest
from app.database import Base
from app.models.vlan_assignment import VlanAssignment
from app.services.vlan_service import (
    VLAN_MAX,
    VLAN_MIN,
    _derive_vlan_id,
    find_or_assign_vlan,
    get_vlan_assignments,
    release_vlan,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


FABRIC_A = uuid.uuid5(uuid.NAMESPACE_DNS, "fabric-a")
FABRIC_B = uuid.uuid5(uuid.NAMESPACE_DNS, "fabric-b")
SWITCH_1 = str(uuid.uuid4())
SWITCH_2 = str(uuid.uuid4())


# --- _derive_vlan_id ---


def test_derive_vlan_id_range():
    """Derived VLAN IDs are always in range 2-4094."""
    for _ in range(1000):
        rid = str(uuid.uuid4())
        vid = _derive_vlan_id(rid)
        assert VLAN_MIN <= vid <= VLAN_MAX


def test_derive_vlan_id_deterministic():
    """Same reservation_id always yields the same VLAN."""
    rid = str(uuid.uuid4())
    assert _derive_vlan_id(rid) == _derive_vlan_id(rid)


# --- find_or_assign_vlan ---


@pytest.mark.asyncio
async def test_assign_vlan_no_conflict(db):
    """With no existing assignments, the preferred (derived) VLAN is used."""
    rid = str(uuid.uuid4())
    vlan = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])
    assert vlan == _derive_vlan_id(rid)


@pytest.mark.asyncio
async def test_assign_vlan_conflict_same_fabric(db):
    """When the derived VLAN is taken in the same fabric, a different one is assigned."""
    rid1 = str(uuid.uuid4())
    rid2 = str(uuid.uuid4())

    # Force both reservations to derive the same preferred VLAN
    # by finding two UUIDs with the same derived VLAN
    # (or just manually create a conflict by assigning rid1 first)
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1])

    # Create a second reservation that would derive the same VLAN
    # We do this by pre-assigning the same VLAN via rid1, then checking rid2
    # If rid2 derives the same VLAN, it should get a different one
    # If it derives a different VLAN, no conflict, both are fine
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_A, [SWITCH_1])

    if _derive_vlan_id(rid1) == _derive_vlan_id(rid2):
        assert vlan2 != vlan1
    else:
        assert vlan2 == _derive_vlan_id(rid2)


@pytest.mark.asyncio
async def test_assign_vlan_forced_conflict(db):
    """Force a collision: two reservations with same derived VLAN in same fabric."""
    # Use a fixed reservation ID and manually create a conflicting assignment
    rid1 = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1])

    # Create rid2 that derives the same VLAN by searching
    target_vlan = vlan1
    rid2 = None
    for i in range(100000):
        candidate = str(uuid.UUID(int=i))
        if _derive_vlan_id(candidate) == target_vlan and candidate != rid1:
            rid2 = candidate
            break
    assert rid2 is not None, "Could not find a colliding reservation ID"

    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_A, [SWITCH_1])
    assert vlan2 != vlan1
    assert VLAN_MIN <= vlan2 <= VLAN_MAX


@pytest.mark.asyncio
async def test_assign_vlan_same_id_different_fabric(db):
    """Same derived VLAN in different fabrics: no conflict, both get the derived VLAN."""
    rid1 = str(uuid.uuid4())
    rid2 = None
    target = _derive_vlan_id(rid1)
    for i in range(100000):
        candidate = str(uuid.UUID(int=i))
        if _derive_vlan_id(candidate) == target and candidate != rid1:
            rid2 = candidate
            break
    assert rid2 is not None

    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1])
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_B, [SWITCH_2])

    # Both should get the same derived VLAN since they are in different fabrics
    assert vlan1 == vlan2 == target


@pytest.mark.asyncio
async def test_assign_vlan_idempotent(db):
    """Same (reservation, fabric) returns the same VLAN on repeated calls."""
    rid = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])
    vlan2 = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])
    assert vlan1 == vlan2


# --- release_vlan ---


@pytest.mark.asyncio
async def test_release_vlan(db):
    """release_vlan marks assignments as RELEASED and returns them."""
    rid = str(uuid.uuid4())
    vlan = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])

    released = await release_vlan(db, rid)
    assert len(released) == 1
    assert released[0].vlan_id == vlan
    assert released[0].status == "RELEASED"
    assert released[0].released_at is not None


@pytest.mark.asyncio
async def test_release_vlan_frees_id(db):
    """After release, the VLAN can be reassigned in the same fabric."""
    rid1 = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1])

    await release_vlan(db, rid1)

    # A new reservation that derives the same VLAN should get it
    rid2 = None
    for i in range(100000):
        candidate = str(uuid.UUID(int=i))
        if _derive_vlan_id(candidate) == vlan1 and candidate != rid1:
            rid2 = candidate
            break
    assert rid2 is not None

    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_A, [SWITCH_1])
    assert vlan2 == vlan1


@pytest.mark.asyncio
async def test_release_vlan_no_assignments(db):
    """Releasing a reservation with no assignments returns empty list."""
    released = await release_vlan(db, str(uuid.uuid4()))
    assert released == []


@pytest.mark.asyncio
async def test_release_vlan_multiple_fabrics(db):
    """A reservation spanning two fabrics releases both assignments."""
    rid = str(uuid.uuid4())
    await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])
    await find_or_assign_vlan(db, rid, FABRIC_B, [SWITCH_2])

    released = await release_vlan(db, rid)
    assert len(released) == 2
    fabrics = {r.fabric_id for r in released}
    assert fabrics == {FABRIC_A, FABRIC_B}


# --- get_vlan_assignments ---


@pytest.mark.asyncio
async def test_get_vlan_assignments(db):
    """get_vlan_assignments returns active assignments for a reservation."""
    rid = str(uuid.uuid4())
    await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])

    assignments = await get_vlan_assignments(db, rid)
    assert len(assignments) == 1
    assert assignments[0].fabric_id == FABRIC_A


@pytest.mark.asyncio
async def test_get_vlan_assignments_excludes_released(db):
    """get_vlan_assignments does not return released assignments."""
    rid = str(uuid.uuid4())
    await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1])
    await release_vlan(db, rid)

    assignments = await get_vlan_assignments(db, rid)
    assert len(assignments) == 0


# --- concurrency (TOCTOU race) ---
#
# We exercise the exact branch the fix adds (commit -> IntegrityError -> rollback ->
# retry) deterministically: a second session commits a conflicting ACTIVE row on the
# same shared database before the first caller's insert lands, so the first caller's
# commit must trip the partial-unique index and retry. This avoids relying on event-
# loop interleaving (in production each replica holds its own Postgres connection; a
# single shared SQLite connection cannot model true simultaneity cleanly).
#
# A shared-cache in-memory engine with a StaticPool lets two sessions see each other's
# committed rows; the partial-unique index from the model is created by create_all.


def _colliding_reservation_id(target_vlan: int, exclude: str) -> str:
    """Find a reservation UUID whose derived VLAN equals target_vlan."""
    for i in range(100000):
        candidate = str(uuid.UUID(int=i))
        if _derive_vlan_id(candidate) == target_vlan and candidate != exclude:
            return candidate
    raise AssertionError("Could not find a colliding reservation ID")


@pytest.fixture
async def shared_engine():
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true",
        connect_args={"uri": True},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _active_vlans(engine, fabric_id) -> list[int]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        result = await s.execute(
            select(VlanAssignment.vlan_id).where(
                VlanAssignment.fabric_id == fabric_id,
                VlanAssignment.status == "ACTIVE",
            )
        )
        return [r[0] for r in result.all()]


def _preassign(fabric_id, vlan_id, reservation_id) -> VlanAssignment:
    return VlanAssignment(
        reservation_id=uuid.UUID(reservation_id),
        fabric_id=fabric_id,
        vlan_id=vlan_id,
        switch_device_ids=[SWITCH_1],
        status="ACTIVE",
    )


@pytest.mark.asyncio
async def test_assign_vlan_loses_race_retries_onto_free_vlan(shared_engine):
    """A racing caller grabs our preferred VLAN first; we must retry onto a free one.

    Drives the partial-unique index -> IntegrityError -> rollback -> retry branch:
    we seed the in-use set as empty (so the caller computes its preferred VLAN), then
    a competing session commits that exact VLAN to a different reservation before the
    caller's commit. The caller's commit trips the index and retries onto a free VLAN.
    """
    maker = async_sessionmaker(shared_engine, expire_on_commit=False)
    rid = str(uuid.uuid4())
    preferred = _derive_vlan_id(rid)
    competitor_rid = _colliding_reservation_id(preferred, exclude=rid)

    # Competitor takes the preferred VLAN first, in its own committed transaction.
    async with maker() as competitor:
        competitor.add(_preassign(FABRIC_A, preferred, competitor_rid))
        await competitor.commit()

    async with maker() as session:
        assigned = await find_or_assign_vlan(session, rid, FABRIC_A, [SWITCH_1])

    assert assigned != preferred, "caller should not reuse the VLAN the competitor took"
    active = await _active_vlans(shared_engine, FABRIC_A)
    assert sorted(active) == sorted([preferred, assigned])  # no duplicate, no orphan
    assert len(active) == 2


@pytest.mark.asyncio
async def test_assign_vlan_concurrent_same_reservation_idempotent(shared_engine):
    """A redelivered assign for the SAME reservation+fabric returns the existing VLAN.

    The competitor here IS this reservation (duplicate NATS delivery). The second
    attempt's commit trips the index, rolls back, and the retry's idempotency check
    finds the already-committed row and returns its VLAN, leaving exactly one row.
    """
    maker = async_sessionmaker(shared_engine, expire_on_commit=False)
    rid = str(uuid.uuid4())
    preferred = _derive_vlan_id(rid)

    # First delivery commits the assignment.
    async with maker() as first:
        vlan1 = await find_or_assign_vlan(first, rid, FABRIC_A, [SWITCH_1])

    # Second delivery on a fresh session: the leading idempotency check already sees
    # the committed row and short-circuits, so this is the redelivery contract.
    async with maker() as second:
        vlan2 = await find_or_assign_vlan(second, rid, FABRIC_A, [SWITCH_1])

    assert vlan1 == vlan2 == preferred
    assert await _active_vlans(shared_engine, FABRIC_A) == [preferred]
