"""Tests for route_service.py: pinned L3 route assignment (issue #20).

The invariant under test: provision pins exactly the routes passed on first
assignment, and every later read (redelivery, deprovision) returns that pinned
set, never a re-derived one. The ADR 0009 phase 5 ledger functions
(record_route_active/record_route_failed and friends) carry this invariant
today; their idempotency/redelivery/reprovision coverage lives in
test_nats_consumer_l3_reconcile.py alongside the reconcile pass that drives
them.
"""

import uuid

import pytest
from app.database import Base
from app.models.route_assignment import RouteAssignment
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


ROUTES = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]

EDITED_ROUTES = [
    {"destination": "172.16.0.0/16", "next_hop": "192.168.1.254", "interface": "eth2"},
]


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
