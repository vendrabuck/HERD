"""Tests for l2_membership_service.py: the L2 VLAN port-membership projection.

ADR 0009 phase 4 (issue #416). Covers the ACTIVE-row insert and idempotency, the
RELEASED flip, the FAILED write with the #412 build-direction guard, the
membership_needs_remove release-side idempotency gate, the cross-reservation
supersession guard, the allocation-coupling count, and the migration backfill
reconstruction helper. The L2 analogue of test_l1_assignment_service.py.
"""

import uuid
from types import SimpleNamespace

import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from app.services.l1_assignment_service import freeze_reservation_wiring
from app.services.l2_membership_service import (
    FROZEN_JOIN_PENDING_REMOVAL,
    STALE_JOIN_SUPERSEDED_PENDING_REMOVAL,
    compute_backfill_l2_memberships,
    count_active_memberships_for_vlan,
    is_membership_active,
    membership_needs_remove,
    record_l2_failed,
    record_l2_membership_active,
    release_l2_membership,
    supersede_l2_release_if_reclaimed,
)
from app.services.wiring_retry_service import is_retryable_failure
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


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


# --- record_l2_membership_active ---


async def test_record_active_inserts_row(db):
    res, va, sw = _ids()
    row = await record_l2_membership_active(db, res, va, sw, "eth1")
    assert row.status == "ACTIVE"
    assert row.intended == "ACTIVE"
    assert row.port == "eth1"
    assert row.vlan_assignment_id == va


async def test_record_active_idempotent_on_existing_active(db):
    res, va, sw = _ids()
    first = await record_l2_membership_active(db, res, va, sw, "eth1")
    again = await record_l2_membership_active(db, res, va, sw, "eth1")
    assert again.id == first.id
    rows = (await db.execute(select(L2PortAssignment))).scalars().all()
    assert len(rows) == 1


async def test_record_active_reuses_prior_failed_row(db):
    """A join that failed then succeeded flips the FAILED row ACTIVE in place."""
    res, va, sw = _ids()
    failed = await record_l2_failed(db, res, va, sw, "eth1", 2, "boom", intended="ACTIVE")
    assert failed.status == "FAILED"
    active = await record_l2_membership_active(db, res, va, sw, "eth1")
    assert active.id == failed.id, "the same row is flipped, not a parallel row"
    assert active.status == "ACTIVE"
    assert active.last_error is None
    rows = (await db.execute(select(L2PortAssignment))).scalars().all()
    assert len(rows) == 1


# --- release_l2_membership ---


async def test_release_flips_active_to_released(db):
    res, va, sw = _ids()
    await record_l2_membership_active(db, res, va, sw, "eth1")
    released = await release_l2_membership(db, res, sw, "eth1")
    assert released.status == "RELEASED"
    assert released.intended == "RELEASED"
    assert released.released_at is not None


async def test_release_missing_returns_none(db):
    res, _va, sw = _ids()
    assert await release_l2_membership(db, res, sw, "eth1") is None


async def test_release_flips_release_direction_failed_row(db):
    """A retried disconnect that finally confirms flips its FAILED row RELEASED."""
    res, va, sw = _ids()
    await record_l2_failed(db, res, va, sw, "eth1", 1, "boom", intended="RELEASED")
    released = await release_l2_membership(db, res, sw, "eth1")
    assert released.status == "RELEASED"


# --- record_l2_failed and the #412 guard ---


async def test_record_failed_accumulates_attempts(db):
    res, va, sw = _ids()
    await record_l2_failed(db, res, va, sw, "eth1", 2, "boom1", intended="ACTIVE")
    row = await record_l2_failed(db, res, va, sw, "eth1", 3, "boom2", intended="ACTIVE")
    assert row.attempts == 5
    assert row.last_error == "boom2"


async def test_412_guard_active_row_immutable_to_build_failure(db):
    """A build failure never downgrades a row a concurrent writer proved ACTIVE."""
    res, va, sw = _ids()
    active = await record_l2_membership_active(db, res, va, sw, "eth1")
    result = await record_l2_failed(db, res, va, sw, "eth1", 4, "stale", intended="ACTIVE")
    assert result.id == active.id
    assert result.status == "ACTIVE", "the ACTIVE winner is not downgraded"
    assert result.attempts == 0, "attempts are not inflated by the refused write"
    assert result.last_error is None


async def test_412_guard_does_not_block_release_direction_failure(db):
    """A release failure on an ACTIVE membership DOES record FAILED (issue #369)."""
    res, va, sw = _ids()
    await record_l2_membership_active(db, res, va, sw, "eth1")
    result = await record_l2_failed(db, res, va, sw, "eth1", 1, "leave boom", intended="RELEASED")
    assert result.status == "FAILED"
    assert result.intended == "RELEASED"


async def test_record_failed_present_key_falsy_is_a_failure_semantics(db):
    """A FAILED write with no resolved allocation still records (nil placeholder)."""
    res, _va, sw = _ids()
    row = await record_l2_failed(db, res, None, sw, "eth1", 0, "no alloc", intended="ACTIVE")
    assert row.status == "FAILED"
    assert row.vlan_assignment_id == uuid.UUID(int=0)


# --- is_membership_active / membership_needs_remove ---


async def test_is_membership_active(db):
    res, va, sw = _ids()
    assert await is_membership_active(db, res, sw, "eth1") is False
    await record_l2_membership_active(db, res, va, sw, "eth1")
    assert await is_membership_active(db, res, sw, "eth1") is True


async def test_membership_needs_remove_true_for_active(db):
    res, va, sw = _ids()
    await record_l2_membership_active(db, res, va, sw, "eth1")
    assert await membership_needs_remove(db, res, sw, "eth1") is True


async def test_membership_needs_remove_true_for_failed_release(db):
    res, va, sw = _ids()
    await record_l2_failed(db, res, va, sw, "eth1", 1, "boom", intended="RELEASED")
    assert await membership_needs_remove(db, res, sw, "eth1") is True


async def test_membership_needs_remove_false_for_failed_build(db):
    """A join that never applied has nothing live to remove."""
    res, va, sw = _ids()
    await record_l2_failed(db, res, va, sw, "eth1", 1, "boom", intended="ACTIVE")
    assert await membership_needs_remove(db, res, sw, "eth1") is False


async def test_membership_needs_remove_false_when_absent(db):
    res, _va, sw = _ids()
    assert await membership_needs_remove(db, res, sw, "eth1") is False


# --- supersede_l2_release_if_reclaimed ---


async def test_supersede_when_other_reservation_active_on_same_port(db):
    va1, va2 = uuid.uuid4(), uuid.uuid4()
    res1, res2 = uuid.uuid4(), uuid.uuid4()
    sw = uuid.uuid4()
    # res2 now holds the port ACTIVE (a re-wire reclaimed it).
    await record_l2_membership_active(db, res2, va2, sw, "eth1")
    stale = await record_l2_failed(db, res1, va1, sw, "eth1", 1, "boom", intended="RELEASED")
    superseded = await supersede_l2_release_if_reclaimed(db, stale)
    assert superseded is True
    await db.refresh(stale)
    assert stale.status == "RELEASED"


async def test_supersede_false_when_no_other_reservation(db):
    res, va, sw = _ids()
    stale = await record_l2_failed(db, res, va, sw, "eth1", 1, "boom", intended="RELEASED")
    assert await supersede_l2_release_if_reclaimed(db, stale) is False


# --- count_active_memberships_for_vlan (allocation coupling) ---


async def test_count_active_memberships_tracks_allocation_lifecycle(db):
    res, va, sw = _ids()
    assert await count_active_memberships_for_vlan(db, va) == 0
    await record_l2_membership_active(db, res, va, sw, "eth1")
    await record_l2_membership_active(db, res, va, sw, "eth2")
    assert await count_active_memberships_for_vlan(db, va) == 2
    await release_l2_membership(db, res, sw, "eth1")
    assert await count_active_memberships_for_vlan(db, va) == 1
    await release_l2_membership(db, res, sw, "eth2")
    assert await count_active_memberships_for_vlan(db, va) == 0


# --- compute_backfill_l2_memberships ---


def _run(res, sw, action, port, created, status="SUCCESS"):
    return SimpleNamespace(
        reservation_id=res,
        device_id=sw,
        action=action,
        status=status,
        port_a=port,
        created_at=created,
    )


def _alloc(va_id, res, sids, status="ACTIVE"):
    return SimpleNamespace(id=va_id, reservation_id=res, switch_device_ids=sids, status=status)


def test_backfill_reconstructs_live_membership():
    res, sw, va = _ids()
    runs = [_run(res, sw, "add_to_vlan", "eth1", 1)]
    allocs = [_alloc(va, res, [str(sw)])]
    result = compute_backfill_l2_memberships(runs, allocs)
    assert result == [
        {"reservation_id": res, "vlan_assignment_id": va, "switch_device_id": sw, "port": "eth1"}
    ]


def test_backfill_skips_removed_membership():
    res, sw, va = _ids()
    runs = [
        _run(res, sw, "add_to_vlan", "eth1", 1),
        _run(res, sw, "remove_from_vlan", "eth1", 2),
    ]
    allocs = [_alloc(va, res, [str(sw)])]
    assert compute_backfill_l2_memberships(runs, allocs) == []


def test_backfill_skips_malformed_run():
    res, sw, va = _ids()
    runs = [_run(res, sw, "add_to_vlan", None, 1), _run(None, sw, "add_to_vlan", "eth1", 1)]
    allocs = [_alloc(va, res, [str(sw)])]
    assert compute_backfill_l2_memberships(runs, allocs) == []


def test_backfill_skips_membership_without_allocation():
    res, sw, _va = _ids()
    runs = [_run(res, sw, "add_to_vlan", "eth1", 1)]
    assert compute_backfill_l2_memberships(runs, []) == []


def test_backfill_ignores_non_success_runs():
    res, sw, va = _ids()
    runs = [_run(res, sw, "add_to_vlan", "eth1", 1, status="FAILED")]
    allocs = [_alloc(va, res, [str(sw)])]
    assert compute_backfill_l2_memberships(runs, allocs) == []


# --- record-time freeze re-check (issue #461) ---


async def test_record_active_frozen_parks_failed_intended_released(db):
    """A join that lands after the wiring freeze is parked FAILED intended RELEASED
    (the record-time analogue of the #412 guard, mirroring record_l1_connect): the
    terminal teardown has already snapshotted the ledger, so an ACTIVE write here
    would strand the membership on the switch forever."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.l2_membership_service import FROZEN_JOIN_PENDING_REMOVAL

    res, va, sw = _ids()
    db.add(ReservationWiringState(reservation_id=res, frozen=True))
    await db.commit()

    row = await record_l2_membership_active(db, res, va, sw, "eth1")
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.vlan_assignment_id == va, "the remove must drive the real allocation"
    assert row.last_error == FROZEN_JOIN_PENDING_REMOVAL
    assert row.attempts == 0

    active = (
        (await db.execute(select(L2PortAssignment).where(L2PortAssignment.status == "ACTIVE")))
        .scalars()
        .all()
    )
    assert active == []


async def test_record_active_frozen_reuses_failed_row_and_keeps_attempts(db):
    """The retry-tick interleaving shape: the FAILED join row being retried is parked in
    place (same row, intended RELEASED, allocation healed) and attempts do not inflate."""
    from app.models.reservation_wiring_state import ReservationWiringState
    from app.services.l2_membership_service import FROZEN_JOIN_PENDING_REMOVAL

    res, va, sw = _ids()
    failed = await record_l2_failed(db, res, None, sw, "eth1", 3, "boom", intended="ACTIVE")
    db.add(ReservationWiringState(reservation_id=res, frozen=True))
    await db.commit()

    row = await record_l2_membership_active(db, res, va, sw, "eth1")
    assert row.id == failed.id, "the same row is parked, not a parallel row"
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.vlan_assignment_id == va
    assert row.attempts == 3
    assert row.last_error == FROZEN_JOIN_PENDING_REMOVAL


# --- record-time supersession of stale cross-reservation rows (issue #479) ---


async def test_record_parks_stale_cross_reservation_active_row(db):
    sw = uuid.uuid4()
    stale_res, stale_va = uuid.uuid4(), uuid.uuid4()
    new_res, new_va = uuid.uuid4(), uuid.uuid4()
    stale = await record_l2_membership_active(db, stale_res, stale_va, sw, "eth1")
    # The stale reservation's teardown froze its wiring (freeze-first, #461) and
    # was then interrupted; only that proves the row condemned.
    await freeze_reservation_wiring(db, stale_res)
    row = await record_l2_membership_active(db, new_res, new_va, sw, "eth1")
    assert row is not None and row.status == "ACTIVE"
    assert row.reservation_id == new_res
    await db.refresh(stale)
    # Parked for the release-direction channels, NOT flipped RELEASED: the join
    # proves port occupancy moved, not that the stale membership left the hardware.
    assert stale.status == "FAILED"
    assert stale.intended == "RELEASED"
    assert stale.attempts == 0
    assert stale.last_error == STALE_JOIN_SUPERSEDED_PENDING_REMOVAL
    assert is_retryable_failure(stale.last_error) is True


async def test_record_parks_every_stale_row_on_the_port(db):
    """Two pre-fix stale ACTIVE rows (built directly: the API can no longer mint
    them) are both parked by one successful join."""
    sw = uuid.uuid4()
    stales = []
    for _ in range(2):
        r = L2PortAssignment(
            reservation_id=uuid.uuid4(),
            vlan_assignment_id=uuid.uuid4(),
            switch_device_id=sw,
            port="eth1",
            status="ACTIVE",
            intended="ACTIVE",
        )
        db.add(r)
        stales.append(r)
    await db.commit()
    for r in stales:
        await freeze_reservation_wiring(db, r.reservation_id)

    await record_l2_membership_active(db, uuid.uuid4(), uuid.uuid4(), sw, "eth1")
    for r in stales:
        await db.refresh(r)
        assert r.status == "FAILED"
        assert r.intended == "RELEASED"
        assert r.last_error == STALE_JOIN_SUPERSEDED_PENDING_REMOVAL


async def test_record_leaves_own_other_vlan_row_alone(db):
    """Scope pin: an own-reservation membership in another VLAN on the same port is
    NOT superseded; the owning reconcile's remove pass converges those."""
    res, sw = uuid.uuid4(), uuid.uuid4()
    old = await record_l2_membership_active(db, res, uuid.uuid4(), sw, "eth1")
    new = await record_l2_membership_active(db, res, uuid.uuid4(), sw, "eth1")
    assert new is not None and new.status == "ACTIVE"
    await db.refresh(old)
    assert old.status == "ACTIVE"
    assert old.intended == "ACTIVE"


async def test_parked_stale_row_settles_superseded_without_driver(db):
    """The full #479 convergence chain at unit level: record-time park, then the
    #424 release-side guard settles the row with no driver call, since the
    superseding reservation's ACTIVE row now holds the port."""
    sw = uuid.uuid4()
    stale_res = uuid.uuid4()
    stale = await record_l2_membership_active(db, stale_res, uuid.uuid4(), sw, "eth1")
    await freeze_reservation_wiring(db, stale_res)
    await record_l2_membership_active(db, uuid.uuid4(), uuid.uuid4(), sw, "eth1")
    await db.refresh(stale)
    assert stale.status == "FAILED" and stale.intended == "RELEASED"
    assert await supersede_l2_release_if_reclaimed(db, stale) is True
    await db.refresh(stale)
    assert stale.status == "RELEASED"


async def test_record_frozen_still_parks_stale_row(db):
    """A join landing on a frozen reservation parks BOTH rows: the stale
    cross-reservation row (issue #479) and its own join (issue #461)."""
    sw = uuid.uuid4()
    new_res = uuid.uuid4()
    stale_res = uuid.uuid4()
    stale = await record_l2_membership_active(db, stale_res, uuid.uuid4(), sw, "eth1")
    await freeze_reservation_wiring(db, stale_res)
    await freeze_reservation_wiring(db, new_res)
    own = await record_l2_membership_active(db, new_res, uuid.uuid4(), sw, "eth1")
    assert own is not None and own.status == "FAILED"
    assert own.intended == "RELEASED"
    assert own.last_error == FROZEN_JOIN_PENDING_REMOVAL
    await db.refresh(stale)
    assert stale.status == "FAILED"
    assert stale.intended == "RELEASED"
    assert stale.last_error == STALE_JOIN_SUPERSEDED_PENDING_REMOVAL


async def test_record_leaves_unfrozen_cross_reservation_row_alone(db):
    """The load-bearing gate (issue #479 review): an UNFROZEN cross-reservation
    ACTIVE row can be the port's rightful live holder (e.g. this join is a
    stale-intent build reattempt, issue #491), so it must never be parked."""
    sw = uuid.uuid4()
    live_res = uuid.uuid4()
    live = await record_l2_membership_active(db, live_res, uuid.uuid4(), sw, "eth1")
    row = await record_l2_membership_active(db, uuid.uuid4(), uuid.uuid4(), sw, "eth1")
    assert row is not None and row.status == "ACTIVE"
    await db.refresh(live)
    assert live.status == "ACTIVE"
    assert live.intended == "ACTIVE"
    assert live.last_error is None
