"""Tests for route_service.py: pinned L3 route assignment (issue #20).

The invariant under test: provision pins exactly the routes passed on first
assignment, and every later read (redelivery, deprovision) returns that pinned
set, never a re-derived one. The partial-unique index on RouteAssignment (at
most one ACTIVE row per reservation+switch, RELEASED rows never block a new
one) is what makes redelivery idempotent; it is pinned directly against the
model here. record_route_active's own redelivery-idempotency behavior (two
calls for the same reservation+switch land one ACTIVE row) is pinned here
too. The ADR 0009 phase 5 reconcile-pass coverage (adjacency-driven
provision/removal, failure gating, the #412 stale-write guard) lives in
test_nats_consumer_l3_reconcile.py.
"""

import uuid

import pytest
from app.database import Base
from app.models.route_assignment import RouteAssignment
from app.services.route_service import record_route_active
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# StaticPool + check_same_thread=False (matching test_wiring_retry_l3.py and
# test_nats_consumer_l3_reconcile.py): the P3 review-fix CAS test below opens
# TWO independent sessions concurrently, which need to share the SAME
# in-memory SQLite database; the default pool gives each checked-out
# connection its own separate `:memory:` database.
test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
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


# --- partial-unique index (at most one ACTIVE row per reservation+switch) ---


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
    rid = uuid.uuid4()
    sid = uuid.uuid4()

    db.add(
        RouteAssignment(
            reservation_id=rid,
            device_id=sid,
            routes=ROUTES,
            status="RELEASED",
        )
    )
    await db.commit()

    db.add(
        RouteAssignment(reservation_id=rid, device_id=sid, routes=EDITED_ROUTES, status="ACTIVE")
    )
    await db.commit()  # does not raise: the RELEASED row does not trip the partial index

    active = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == rid,
                    RouteAssignment.device_id == sid,
                    RouteAssignment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(active) == 1
    assert active[0].routes == EDITED_ROUTES


async def test_record_route_active_is_idempotent_for_redelivery(db):
    """A second record_route_active call for the same (reservation, switch) returns
    the existing ACTIVE row rather than pinning a second one (the redelivery
    guarantee the partial-unique index backstops)."""
    rid = uuid.uuid4()
    sid = uuid.uuid4()

    first = await record_route_active(db, rid, sid, ROUTES)
    second = await record_route_active(db, rid, sid, EDITED_ROUTES)

    assert first.id == second.id
    assert second.routes == ROUTES, "the ORIGINAL pinned set survives, not the redelivered one"

    rows = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == rid,
                    RouteAssignment.device_id == sid,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "ACTIVE"


# --- record-time freeze re-check (issue #461) ---


async def test_record_route_active_frozen_parks_failed_intended_released(db):
    """A provision that lands after the wiring freeze is parked FAILED intended
    RELEASED with the routes pinned (the record-time analogue of the #412 guard,
    mirroring record_l1_connect), so the release-direction retry channels can remove
    exactly the set that was applied."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.route_service import FROZEN_PROVISION_PENDING_REMOVAL

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
    from app.services.route_service import FROZEN_PROVISION_PENDING_REMOVAL, record_route_failed

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


# --- record_route_reconciled / record_route_reconcile_failed (ADR 0014 Decision 3,
# --- issue #34 phase 3): the intent-delta bookkeeping for an already-ACTIVE pin ---


async def test_record_route_reconciled_advances_the_pin_on_an_active_row(db):
    from app.services.route_service import record_route_reconciled

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    row = await record_route_reconciled(db, rid, sid, EDITED_ROUTES, ROUTES)
    assert row.status == "ACTIVE"
    assert row.routes == EDITED_ROUTES, "unlike record_route_active, this DOES advance the pin"


async def test_record_route_reconciled_returns_none_when_no_active_row(db):
    """The caller guarantees a reconcile item targets an already-ACTIVE switch;
    this is a no-op safety net, not an expected path."""
    from app.services.route_service import record_route_reconciled

    row = await record_route_reconciled(db, uuid.uuid4(), uuid.uuid4(), EDITED_ROUTES, ROUTES)
    assert row is None


async def test_record_route_reconciled_frozen_parks_failed_intended_released(db):
    """A reconcile whose reservation froze mid-flight (the record-time race, the
    #412-style re-check) is NOT advanced to the new set: it is parked FAILED
    intended RELEASED with the PRIOR pin left in place for the release channel."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.route_service import FROZEN_PROVISION_PENDING_REMOVAL, record_route_reconciled

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    row = await record_route_reconciled(db, rid, sid, EDITED_ROUTES, ROUTES)
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.routes == ROUTES, "the PRIOR pin survives; the new set is never applied"
    assert row.last_error == FROZEN_PROVISION_PENDING_REMOVAL


async def test_record_route_reconciled_stale_previous_routes_is_a_noop(db):
    """Review fix P2 (issue #34 phase 3 review): when the row's CURRENT routes no
    longer equal `previous_routes` (a faster concurrent writer already moved it),
    this call is a no-op that returns None and never overwrites the row, even
    though an ACTIVE row exists."""
    from app.services.route_service import record_route_reconciled

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    # previous_routes does not match the row's actual current routes (ROUTES).
    row = await record_route_reconciled(db, rid, sid, EDITED_ROUTES, EDITED_ROUTES)
    assert row is None

    survivor = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == rid,
                    RouteAssignment.device_id == sid,
                )
            )
        )
        .scalars()
        .one()
    )
    assert survivor.status == "ACTIVE"
    assert survivor.routes == ROUTES, "the stale writer's call never touched the row"


async def test_record_route_reconciled_matching_previous_routes_succeeds(db):
    """The CAS's positive case: when `previous_routes` matches exactly what is
    currently pinned, the write proceeds normally."""
    from app.services.route_service import record_route_reconciled

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    row = await record_route_reconciled(db, rid, sid, EDITED_ROUTES, ROUTES)
    assert row is not None
    assert row.routes == EDITED_ROUTES


async def test_record_route_reconcile_failed_keeps_previous_pin(db):
    from app.services.route_service import record_route_reconcile_failed

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    row = await record_route_reconcile_failed(db, rid, sid, 2, "boom", ROUTES)
    assert row.status == "FAILED"
    assert row.intended == "ACTIVE"
    assert row.routes == ROUTES, "Decision 3: the previous pinned set survives a failed delta"
    assert row.attempts == 2
    assert row.last_error == "boom"

    # A row this function already flipped FAILED is no longer ACTIVE, so a second
    # call against the same (reservation, switch) is a no-op (None): once FAILED,
    # further reattempts go through the retry channel's own upsert
    # (record_route_failed), not a second reconcile-delta failure.
    row2 = await record_route_reconcile_failed(db, rid, sid, 3, "boom again", ROUTES)
    assert row2 is None


async def test_record_route_reconcile_failed_is_not_guarded_by_412_unlike_record_route_failed(db):
    """The key behavioral difference from record_route_failed's build-direction
    call: an ACTIVE row IS flipped FAILED here (no "a concurrent writer already
    won" guard), since this writer IS the one that owns this pin's history for a
    reconcile delta whose previous_routes still matches."""
    from app.services.route_service import record_route_reconcile_failed

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    row = await record_route_reconcile_failed(db, rid, sid, 1, "delta boom", ROUTES)
    assert row.status == "FAILED", "unlike record_route_failed, the ACTIVE row IS downgraded"


async def test_record_route_reconcile_failed_returns_none_when_not_active(db):
    from app.services.route_service import record_route_reconcile_failed

    row = await record_route_reconcile_failed(db, uuid.uuid4(), uuid.uuid4(), 1, "boom", ROUTES)
    assert row is None


async def test_record_route_reconcile_failed_stale_previous_routes_is_a_noop(db):
    """Review fix P2: a mismatched `previous_routes` (a faster concurrent writer
    already moved the row) is a no-op that never flips the row FAILED, even
    though it is still ACTIVE."""
    from app.services.route_service import record_route_reconcile_failed

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_active(db, rid, sid, ROUTES)

    row = await record_route_reconcile_failed(db, rid, sid, 1, "delta boom", EDITED_ROUTES)
    assert row is None

    survivor = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == rid,
                    RouteAssignment.device_id == sid,
                )
            )
        )
        .scalars()
        .one()
    )
    assert survivor.status == "ACTIVE", "the stale writer's call never flipped the row"
    assert survivor.routes == ROUTES


# --- record_route_active's FAILED-to-ACTIVE flip is a CAS on status (review
# --- fix P3, issue #34 phase 3 review) ---


async def test_record_route_active_reusable_flip_succeeds_when_cas_matches(db):
    """The CAS's positive path: a plain (non-racing) reattempt still flips
    FAILED to ACTIVE with the new routes, since the row's status is still
    FAILED when the UPDATE runs."""
    from app.services.route_service import record_route_failed

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    await record_route_failed(db, rid, sid, ROUTES, 2, "boom", intended="ACTIVE")

    row = await record_route_active(db, rid, sid, EDITED_ROUTES)
    assert row.status == "ACTIVE"
    assert row.routes == EDITED_ROUTES


async def test_record_route_active_reusable_cas_loser_never_overwrites(db):
    """P3 review fix: the FAILED-to-ACTIVE flip's CAS
    (`UPDATE ... WHERE status = 'FAILED'`) must reject a writer whose UPDATE
    runs after another writer's commit already flipped the row, even though
    BOTH writers' SELECTs saw the row as FAILED (the genuine TOCTOU window this
    fix closes: the ordinary consumer path and the wiring-retry sweep can both
    reach record_route_active for the same row as independent asyncio tasks).

    A live two-session interleaving that forces both writers past their SELECT
    before either commits cannot be driven by two SEQUENTIAL calls to
    record_route_active itself: the second caller's own `existing == ACTIVE`
    check would already short-circuit once the first has committed (a
    different, pre-existing safety net that predates phase 3). So this test
    drives the exact CAS UPDATE statement record_route_active issues directly,
    with the interleaving forced explicitly, to prove the CAS clause itself
    (not the unrelated `existing` short-circuit) is what rejects the loser.
    """
    from app.services.route_service import record_route_failed
    from sqlalchemy import update

    rid = uuid.uuid4()
    sid = uuid.uuid4()
    failed = await record_route_failed(db, rid, sid, ROUTES, 2, "boom", intended="ACTIVE")

    async with TestSessionLocal() as db_a, TestSessionLocal() as db_b:
        # Both writers' SELECTs land before either writer's UPDATE (the race).
        row_a = (
            await db_a.execute(select(RouteAssignment).where(RouteAssignment.id == failed.id))
        ).scalar_one()
        row_b = (
            await db_b.execute(select(RouteAssignment).where(RouteAssignment.id == failed.id))
        ).scalar_one()
        assert row_a.status == "FAILED"
        assert row_b.status == "FAILED"

        # Writer A wins: its CAS UPDATE matches (status is still FAILED) and commits.
        result_a = await db_a.execute(
            update(RouteAssignment)
            .where(RouteAssignment.id == failed.id, RouteAssignment.status == "FAILED")
            .values(status="ACTIVE", intended="ACTIVE", routes=ROUTES, last_error=None)
        )
        assert result_a.rowcount == 1
        await db_a.commit()

        # Writer B (the loser) issues the SAME CAS UPDATE against the row it read
        # BEFORE A's commit; its WHERE clause no longer matches (status is now
        # ACTIVE), so it must affect zero rows and must NEVER overwrite A's
        # content.
        result_b = await db_b.execute(
            update(RouteAssignment)
            .where(RouteAssignment.id == failed.id, RouteAssignment.status == "FAILED")
            .values(status="ACTIVE", intended="ACTIVE", routes=EDITED_ROUTES, last_error=None)
        )
        assert result_b.rowcount == 0, "the loser's CAS must match zero rows"
        await db_b.rollback()

    final = (
        await db.execute(select(RouteAssignment).where(RouteAssignment.id == failed.id))
    ).scalar_one()
    assert final.routes == ROUTES, "the winner's content survives; the loser never overwrote it"
