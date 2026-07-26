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
from app.services.l2_membership_service import (
    compute_backfill_l2_memberships,
    count_active_memberships_for_vlan,
    is_membership_active,
    membership_needs_remove,
    record_l2_failed,
    record_l2_membership_active,
    release_l2_membership,
    supersede_l2_release_if_reclaimed,
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
