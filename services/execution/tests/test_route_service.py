"""Tests for route_service.py: pinned L3 route assignment (issue #20).

The invariant under test: provision pins exactly the routes passed on first
assignment, and every later read (redelivery, deprovision) returns that pinned
set, never a re-derived one.
"""

import uuid

import pytest
from app.database import Base
from app.models.route_assignment import RouteAssignment
from app.services.route_service import (
    assign_routes,
    release_routes,
    release_routes_for_device,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


ROUTES = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]

EDITED_ROUTES = [
    {"destination": "172.16.0.0/16", "next_hop": "192.168.1.254", "interface": "eth2"},
]


async def test_assign_routes_inserts_active_row(db):
    """First assignment persists an ACTIVE row carrying the exact route list."""
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    pinned = await assign_routes(db, rid, sid, ROUTES)
    assert pinned == ROUTES

    row = (
        await db.execute(
            select(RouteAssignment).where(
                RouteAssignment.reservation_id == uuid.UUID(rid),
                RouteAssignment.device_id == uuid.UUID(sid),
            )
        )
    ).scalar_one()
    assert row.status == "ACTIVE"
    assert row.routes == ROUTES
    assert row.released_at is None
    # ADR 0009 Decision 2 additive columns (issue #369/#416): route_service does
    # not write these yet (that is the L3 layered reconcile, phase 5), so a
    # freshly assigned row gets their defaults.
    assert row.attempts == 0
    assert row.last_error is None
    assert row.intended == "ACTIVE"


def test_route_assignment_status_accepts_failed_value():
    """Additive schema change (issue #369/#416): "FAILED" is now a legal status
    value, even though route_service does not write it this phase."""
    row = RouteAssignment(
        reservation_id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        routes=[],
        status="FAILED",
        attempts=2,
        last_error="boom",
        intended="ACTIVE",
    )
    assert row.status == "FAILED"
    assert row.attempts == 2
    assert row.last_error == "boom"


async def test_assign_routes_is_idempotent_for_redelivery(db):
    """A second assignment returns the ORIGINAL pinned set, not the new argument.

    This is the redelivery guarantee: if the switch's config is edited between
    two deliveries of the same event, the replay still provisions what the
    first delivery pinned.
    """
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    first = await assign_routes(db, rid, sid, ROUTES)
    second = await assign_routes(db, rid, sid, EDITED_ROUTES)

    assert first == ROUTES
    assert second == ROUTES

    rows = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == uuid.UUID(rid),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_assign_routes_retries_after_integrity_error(db):
    """A commit that trips the unique index rolls back and retries to success."""
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    real_commit = db.commit
    calls = {"n": 0}

    async def flaky_commit():
        if calls["n"] == 0:
            calls["n"] += 1
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        await real_commit()

    db.commit = flaky_commit
    pinned = await assign_routes(db, rid, sid, ROUTES)
    assert pinned == ROUTES
    assert calls["n"] == 1


async def test_partial_unique_index_blocks_duplicate_active_insert(db):
    """The DB, not the service, is the final arbiter against double-pinning."""
    rid = uuid.uuid4()
    sid = uuid.uuid4()

    db.add(RouteAssignment(reservation_id=rid, device_id=sid, routes=ROUTES, status="ACTIVE"))
    await db.commit()

    db.add(RouteAssignment(reservation_id=rid, device_id=sid, routes=ROUTES, status="ACTIVE"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_released_row_does_not_block_new_assignment(db):
    """The unique predicate is ACTIVE-only, so history rows never block."""
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    await assign_routes(db, rid, sid, ROUTES)
    await release_routes(db, rid)

    pinned = await assign_routes(db, rid, sid, EDITED_ROUTES)
    assert pinned == EDITED_ROUTES


async def test_release_routes_marks_released_and_returns_routes(db):
    """Release returns the pinned assignments and stamps released_at."""
    rid = str(uuid.uuid4())
    sid1 = str(uuid.uuid4())
    sid2 = str(uuid.uuid4())

    await assign_routes(db, rid, sid1, ROUTES)
    await assign_routes(db, rid, sid2, EDITED_ROUTES)

    released = await release_routes(db, rid)
    assert len(released) == 2
    by_device = {str(a.device_id): a for a in released}
    assert by_device[sid1].routes == ROUTES
    assert by_device[sid2].routes == EDITED_ROUTES
    for a in released:
        assert a.status == "RELEASED"
        assert a.released_at is not None


async def test_release_routes_empty_is_noop(db):
    """Releasing a reservation with no assignments returns an empty list."""
    released = await release_routes(db, str(uuid.uuid4()))
    assert released == []


async def test_release_routes_is_idempotent(db):
    """A second release (redelivered cancel event) finds nothing ACTIVE."""
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    await assign_routes(db, rid, sid, ROUTES)
    first = await release_routes(db, rid)
    second = await release_routes(db, rid)
    assert len(first) == 1
    assert second == []


async def test_release_routes_for_device_scopes_to_one_switch(db):
    """Per-device release leaves the other switch's assignment ACTIVE."""
    rid = str(uuid.uuid4())
    sid1 = str(uuid.uuid4())
    sid2 = str(uuid.uuid4())

    await assign_routes(db, rid, sid1, ROUTES)
    await assign_routes(db, rid, sid2, EDITED_ROUTES)

    released = await release_routes_for_device(db, rid, sid1)
    assert len(released) == 1
    assert str(released[0].device_id) == sid1

    remaining = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == uuid.UUID(rid),
                    RouteAssignment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1
    assert str(remaining[0].device_id) == sid2


# --- record-time freeze re-check (issue #461) ---


async def test_record_route_active_frozen_parks_failed_intended_released(db):
    """A provision that lands after the wiring freeze is parked FAILED intended
    RELEASED with the routes pinned (the record-time analogue of the #412 guard,
    mirroring record_l1_connect), so the release-direction retry channels can remove
    exactly the set that was applied."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.route_service import (
        FROZEN_PROVISION_PENDING_REMOVAL,
        record_route_active,
    )

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    row = await record_route_active(db, rid, sid, ROUTES)
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.routes == ROUTES, "the pinned set survives so the removal drives it verbatim"
    assert row.last_error == FROZEN_PROVISION_PENDING_REMOVAL
    assert row.attempts == 0

    active = (
        (await db.execute(select(RouteAssignment).where(RouteAssignment.status == "ACTIVE")))
        .scalars()
        .all()
    )
    assert active == []


async def test_record_route_active_frozen_reuses_failed_row_keeps_pinned_routes(db):
    """The retry-tick interleaving shape: the FAILED provision row being retried is
    parked in place, keeping its already-pinned routes (the immutable set) and its
    accumulated attempts."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.route_service import (
        FROZEN_PROVISION_PENDING_REMOVAL,
        record_route_active,
        record_route_failed,
    )

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    failed = await record_route_failed(db, rid, sid, ROUTES, 2, "boom", intended="ACTIVE")
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    row = await record_route_active(db, rid, sid, EDITED_ROUTES)
    assert row.id == failed.id, "the same row is parked, not a parallel row"
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.routes == ROUTES, "the flip keeps the original pinned set, never the edit"
    assert row.attempts == 2
    assert row.last_error == FROZEN_PROVISION_PENDING_REMOVAL
