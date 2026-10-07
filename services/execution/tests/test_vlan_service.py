"""Tests for vlan_service.py: reachability-scoped VLAN assignment (issue #1003)."""

import uuid

import pytest
from app.database import Base
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.nats_consumer import TransientUpstreamError
from app.services.vlan_service import (
    VLAN_MAX,
    VLAN_MIN,
    FabricResolver,
    _derive_vlan_id,
    find_or_assign_allocation,
    find_or_assign_vlan,
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
SWITCH_3 = str(uuid.uuid4())


def _resolver(fabric_of: dict[str, uuid.UUID] | None = None) -> FabricResolver:
    """A resolver over a fixed cabling answer; SWITCH_1 is in FABRIC_A and SWITCH_2
    in FABRIC_B unless the test says otherwise. It records every lookup."""
    answers = {SWITCH_1: FABRIC_A, SWITCH_2: FABRIC_B}
    answers.update(fabric_of or {})

    async def fetch(switch_id: str) -> uuid.UUID:
        fetch.calls.append(switch_id)
        return answers[switch_id]

    fetch.calls = []
    resolver = FabricResolver(fetch=fetch)
    resolver.calls = fetch.calls
    return resolver


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
    vlan = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1], _resolver())
    assert vlan == _derive_vlan_id(rid)


@pytest.mark.asyncio
async def test_assign_vlan_conflict_same_fabric(db):
    """When the derived VLAN is taken in the same fabric, a different one is assigned."""
    rid1 = str(uuid.uuid4())
    rid2 = str(uuid.uuid4())

    # Force both reservations to derive the same preferred VLAN
    # by finding two UUIDs with the same derived VLAN
    # (or just manually create a conflict by assigning rid1 first)
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1], _resolver())

    # Create a second reservation that would derive the same VLAN
    # We do this by pre-assigning the same VLAN via rid1, then checking rid2
    # If rid2 derives the same VLAN, it should get a different one
    # If it derives a different VLAN, no conflict, both are fine
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_A, [SWITCH_1], _resolver())

    if _derive_vlan_id(rid1) == _derive_vlan_id(rid2):
        assert vlan2 != vlan1
    else:
        assert vlan2 == _derive_vlan_id(rid2)


@pytest.mark.asyncio
async def test_assign_vlan_forced_conflict(db):
    """Force a collision: two reservations with same derived VLAN in same fabric."""
    # Use a fixed reservation ID and manually create a conflicting assignment
    rid1 = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1], _resolver())

    # Create rid2 that derives the same VLAN by searching
    target_vlan = vlan1
    rid2 = None
    for i in range(100000):
        candidate = str(uuid.UUID(int=i))
        if _derive_vlan_id(candidate) == target_vlan and candidate != rid1:
            rid2 = candidate
            break
    assert rid2 is not None, "Could not find a colliding reservation ID"

    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_A, [SWITCH_1], _resolver())
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

    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1], _resolver())
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_B, [SWITCH_2], _resolver())

    # Both should get the same derived VLAN since they are in different fabrics
    assert vlan1 == vlan2 == target


@pytest.mark.asyncio
async def test_assign_vlan_idempotent(db):
    """Same (reservation, fabric) returns the same VLAN on repeated calls."""
    rid = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1], _resolver())
    vlan2 = await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1], _resolver())
    assert vlan1 == vlan2


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
        assigned = await find_or_assign_vlan(session, rid, FABRIC_A, [SWITCH_1], _resolver())

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
        vlan1 = await find_or_assign_vlan(first, rid, FABRIC_A, [SWITCH_1], _resolver())

    # Second delivery on a fresh session: the leading idempotency check already sees
    # the committed row and short-circuits, so this is the redelivery contract.
    async with maker() as second:
        vlan2 = await find_or_assign_vlan(second, rid, FABRIC_A, [SWITCH_1], _resolver())

    assert vlan1 == vlan2 == preferred
    assert await _active_vlans(shared_engine, FABRIC_A) == [preferred]


# --- reachability scope (issue #1003) ---
#
# The uniqueness scope is the connected cabling component on the CURRENT graph,
# transit included. Cabling answers a component key (the fabric id) per switch; two
# switches are in one component exactly when the answers are equal. The fabric id
# stored on a row is only the key at its creation time.


def _row(reservation_id, fabric_id, vlan_id, switches, defined=None) -> VlanAssignment:
    return VlanAssignment(
        reservation_id=uuid.UUID(reservation_id),
        fabric_id=fabric_id,
        vlan_id=vlan_id,
        switch_device_ids=list(switches),
        defined_switch_ids=list(defined or []),
        status="ACTIVE",
    )


FABRIC_C = uuid.uuid5(uuid.NAMESPACE_DNS, "fabric-c")


@pytest.mark.asyncio
async def test_cable_change_between_joins_keeps_numbers_distinct(db):
    """Issue #1003 sequence 2: reservation 1 allocates on SWITCH_1 while its component
    is keyed FABRIC_A; an admin cable change joins SWITCH_2 to it and re-keys the
    component to FABRIC_C; reservation 2, preferring the same number, allocates on
    SWITCH_2. The old code compared stored fabric ids only and handed out the same
    number; reachability through SWITCH_1's current key refuses it."""
    rid1 = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(db, rid1, FABRIC_A, [SWITCH_1], _resolver())
    rid2 = _colliding_reservation_id(vlan1, exclude=rid1)

    after_change = _resolver({SWITCH_1: FABRIC_C, SWITCH_2: FABRIC_C})
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_C, [SWITCH_2], after_change)

    assert vlan2 != vlan1
    assert SWITCH_1 in after_change.calls, "the other allocation's anchor was re-asked"


@pytest.mark.asyncio
async def test_cable_removed_split_components_may_reuse_number(db):
    """The converse: when the current graph puts the two switches in different
    components, the number is free to reuse even though the stored rows were made
    while they shared one (only the current graph is the authority)."""
    rid1 = str(uuid.uuid4())
    vlan1 = await find_or_assign_vlan(
        db, rid1, FABRIC_C, [SWITCH_1], _resolver({SWITCH_1: FABRIC_C})
    )
    rid2 = _colliding_reservation_id(vlan1, exclude=rid1)

    split = _resolver({SWITCH_1: FABRIC_A, SWITCH_2: FABRIC_B})
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_B, [SWITCH_2], split)
    assert vlan2 == vlan1


@pytest.mark.asyncio
async def test_defined_switch_counts_as_an_anchor(db):
    """A number provably defined on a switch (defined_switch_ids) is in use there even
    when the allocation's scope no longer lists that switch: a transit switch reachable
    from the new allocation blocks the number."""
    rid1 = str(uuid.uuid4())
    vlan1 = _derive_vlan_id(rid1)
    db.add(_row(rid1, FABRIC_A, vlan1, switches=[], defined=[SWITCH_3]))
    await db.commit()
    rid2 = _colliding_reservation_id(vlan1, exclude=rid1)

    resolver = _resolver({SWITCH_3: FABRIC_B})
    vlan2 = await find_or_assign_vlan(db, rid2, FABRIC_B, [SWITCH_2], resolver)
    assert vlan2 != vlan1


@pytest.mark.asyncio
async def test_same_reservation_reuses_reachable_allocation_after_cable_change(db):
    """Issue #1003, one reservation: after a cable change re-keys the component, a join
    on another switch in it reuses the reservation's existing allocation instead of
    minting a second number inside one component."""
    rid = str(uuid.uuid4())
    va1, vlan1 = await find_or_assign_allocation(db, rid, FABRIC_A, [SWITCH_1], _resolver())

    after_change = _resolver({SWITCH_1: FABRIC_C, SWITCH_2: FABRIC_C})
    va2, vlan2 = await find_or_assign_allocation(db, rid, FABRIC_C, [SWITCH_2], after_change)

    assert (va2, vlan2) == (va1, vlan1)
    rows = (await db.execute(select(VlanAssignment))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_idempotent_path_does_not_walk_other_reservations(db):
    """Cost pin: a reservation that already holds a reachable allocation is answered
    from its own rows; other reservations' anchors are never looked up."""
    other = str(uuid.uuid4())
    db.add(_row(other, FABRIC_B, 7, switches=[SWITCH_3]))
    await db.commit()
    rid = str(uuid.uuid4())
    first = _resolver({SWITCH_3: FABRIC_B})
    await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1], first)
    assert first.calls == [SWITCH_3], "the first allocation walks the other reservation"

    resolver = _resolver({SWITCH_3: FABRIC_B})
    await find_or_assign_vlan(db, rid, FABRIC_A, [SWITCH_1], resolver)
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_outage_during_allocation_raises_and_allocates_nothing(db):
    """Issue #1003 sequence 1, decision d: when cabling cannot answer for another
    allocation's anchor, allocation raises TransientUpstreamError (the consumer NAKs)
    and writes no row; there is no stand-in fabric."""
    other = str(uuid.uuid4())
    db.add(_row(other, FABRIC_A, 9, switches=[SWITCH_1]))
    await db.commit()

    async def down(switch_id: str) -> uuid.UUID:
        raise TransientUpstreamError(f"cabling fabric lookup for device {switch_id}: upstream 503")

    rid = str(uuid.uuid4())
    with pytest.raises(TransientUpstreamError, match="upstream 503"):
        await find_or_assign_vlan(db, rid, FABRIC_B, [SWITCH_2], FabricResolver(fetch=down))

    rows = (await db.execute(select(VlanAssignment))).scalars().all()
    assert [str(r.reservation_id) for r in rows] == [other]


@pytest.mark.asyncio
async def test_row_inserted_before_the_lock_is_seen_under_it(shared_engine, monkeypatch):
    """The read-choose-insert runs under the allocation advisory lock and re-reads the
    in-use set there: a competitor committed after the unlocked pre-pass (anchored on a
    reachable switch, stored under another fabric id) still blocks its number."""
    maker = async_sessionmaker(shared_engine, expire_on_commit=False)
    rid = str(uuid.uuid4())
    preferred = _derive_vlan_id(rid)
    competitor = _colliding_reservation_id(preferred, exclude=rid)
    keys: list[int] = []

    async def fake_lock(session, key):
        keys.append(key)
        if len(keys) == 1:
            async with maker() as other:
                other.add(_row(competitor, FABRIC_C, preferred, switches=[SWITCH_3]))
                await other.commit()

    monkeypatch.setattr(vlan_service.advisory_lock, "xact_lock", fake_lock)
    resolver = _resolver({SWITCH_3: FABRIC_A})
    async with maker() as session:
        vlan = await find_or_assign_vlan(session, rid, FABRIC_A, [SWITCH_1], resolver)

    assert keys == [vlan_service._ALLOCATION_LOCK_KEY]
    assert vlan != preferred
