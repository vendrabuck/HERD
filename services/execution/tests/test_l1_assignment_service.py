"""Tests for l1_assignment_service.py: the L1 applied-state projection.

ADR 0007 Decision 4 (issue #345 P3b phase 1). Covers the ACTIVE-row insert, the
RELEASED flip, port-pair canonicalization, the active-unique IntegrityError race
(pinned the way the vlan tests pin theirs), FAILED rows not blocking a new claim,
the frozen setter, and the migration backfill reconstruction helper.
"""

import uuid
from types import SimpleNamespace

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.services import l1_assignment_service as svc
from app.services.l1_assignment_service import (
    canonical_port_pair,
    compute_backfill_assignments,
    freeze_reservation_wiring,
    record_l1_connect,
    release_l1_connection,
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


# --- canonical_port_pair ---


def test_canonical_port_pair_orders_deterministically():
    assert canonical_port_pair("0/0/2", "0/0/1") == ("0/0/1", "0/0/2")
    assert canonical_port_pair("0/0/1", "0/0/2") == ("0/0/1", "0/0/2")


def test_canonical_port_pair_both_orders_collide():
    """The two directed orders of one physical pair map to the same tuple."""
    assert canonical_port_pair("A", "B") == canonical_port_pair("B", "A")


# --- record_l1_connect ---


@pytest.mark.asyncio
async def test_record_connect_inserts_active_row(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()

    row = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    assert row is not None
    assert row.status == "ACTIVE"
    assert row.reservation_id == rid
    assert row.switch_device_id == switch
    assert (row.port_a, row.port_b) == ("0/0/1", "0/0/2")
    assert row.attempts == 0
    assert row.physical_connection_id is None
    assert row.released_at is None


@pytest.mark.asyncio
async def test_record_connect_canonicalizes_reversed_order(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()

    row = await record_l1_connect(db, rid, switch, "0/0/2", "0/0/1")
    assert (row.port_a, row.port_b) == ("0/0/1", "0/0/2")


@pytest.mark.asyncio
async def test_record_connect_reversed_order_is_idempotent(db):
    """A redelivery in the reversed port order finds the existing ACTIVE row."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()

    first = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    second = await record_l1_connect(db, rid, switch, "0/0/2", "0/0/1")

    assert second.id == first.id
    rows = (await db.execute(select(L1ConnectionAssignment))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_duplicate_active_insert(db):
    """The DB is the final arbiter against two live cross-connects on one pair."""
    switch = uuid.uuid4()
    db.add(
        L1ConnectionAssignment(
            reservation_id=uuid.uuid4(),
            switch_device_id=switch,
            port_a="A",
            port_b="B",
            status="ACTIVE",
        )
    )
    await db.commit()

    db.add(
        L1ConnectionAssignment(
            reservation_id=uuid.uuid4(),
            switch_device_id=switch,
            port_a="A",
            port_b="B",
            status="ACTIVE",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_failed_row_does_not_block_new_active_claim(db):
    """The unique predicate is ACTIVE-only, so a FAILED row never blocks a claim."""
    switch = uuid.uuid4()
    db.add(
        L1ConnectionAssignment(
            reservation_id=uuid.uuid4(),
            switch_device_id=switch,
            port_a="A",
            port_b="B",
            status="FAILED",
        )
    )
    await db.commit()

    row = await record_l1_connect(db, uuid.uuid4(), switch, "A", "B")
    assert row is not None
    assert row.status == "ACTIVE"

    actives = (
        (
            await db.execute(
                select(L1ConnectionAssignment).where(
                    L1ConnectionAssignment.switch_device_id == switch,
                    L1ConnectionAssignment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(actives) == 1


# --- release_l1_connection ---


@pytest.mark.asyncio
async def test_release_flips_active_to_released(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")

    released = await release_l1_connection(db, rid, switch, "0/0/1", "0/0/2")
    assert released is not None
    assert released.status == "RELEASED"
    assert released.released_at is not None


@pytest.mark.asyncio
async def test_release_matches_reversed_port_order(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")

    released = await release_l1_connection(db, rid, switch, "0/0/2", "0/0/1")
    assert released is not None
    assert released.status == "RELEASED"


@pytest.mark.asyncio
async def test_release_frees_the_pair_for_a_new_claim(db):
    """After release the pair can be claimed ACTIVE again (index is ACTIVE-only)."""
    rid1 = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid1, switch, "A", "B")
    await release_l1_connection(db, rid1, switch, "A", "B")

    row = await record_l1_connect(db, uuid.uuid4(), switch, "A", "B")
    assert row is not None
    assert row.status == "ACTIVE"


@pytest.mark.asyncio
async def test_release_no_matching_active_row_is_noop(db):
    released = await release_l1_connection(db, uuid.uuid4(), uuid.uuid4(), "A", "B")
    assert released is None


# --- active-unique IntegrityError race ---
#
# Mirrors the vlan tests: a competing connect commits the ACTIVE row for the pair
# first, and the caller's insert trips the partial-unique index, rolls back, and
# returns the winner's row. Because _find_active is keyed on the pair (not the
# reservation), a committed conflict is normally seen by the leading read; to
# exercise the DB-arbitrated except branch deterministically we force only the
# leading read to miss, then let the real DB index reject the insert.


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


async def _active_pairs(engine, switch) -> list[tuple]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        result = await s.execute(
            select(L1ConnectionAssignment.port_a, L1ConnectionAssignment.port_b).where(
                L1ConnectionAssignment.switch_device_id == switch,
                L1ConnectionAssignment.status == "ACTIVE",
            )
        )
        return [(r[0], r[1]) for r in result.all()]


@pytest.mark.asyncio
async def test_record_connect_loses_race_returns_winner(shared_engine, monkeypatch):
    """A racing connect claims the pair first; our insert trips the active-unique
    index, we roll back, and we return the winner's committed row, leaving exactly
    one ACTIVE row."""
    maker = async_sessionmaker(shared_engine, expire_on_commit=False)
    switch = uuid.uuid4()
    winner_res = uuid.uuid4()
    loser_res = uuid.uuid4()

    async with maker() as competitor:
        competitor.add(
            L1ConnectionAssignment(
                reservation_id=winner_res,
                switch_device_id=switch,
                port_a="A",
                port_b="B",
                status="ACTIVE",
            )
        )
        await competitor.commit()

    real_find = svc._find_active
    calls = {"n": 0}

    async def find_miss_once(db, s, pa, pb):
        if calls["n"] == 0:
            calls["n"] += 1
            return None
        return await real_find(db, s, pa, pb)

    monkeypatch.setattr(svc, "_find_active", find_miss_once)

    async with maker() as session:
        row = await record_l1_connect(session, loser_res, switch, "A", "B")

    assert row is not None
    assert row.reservation_id == winner_res, "caller must return the winner, not itself"
    assert await _active_pairs(shared_engine, switch) == [("A", "B")]


# --- freeze_reservation_wiring ---


@pytest.mark.asyncio
async def test_freeze_inserts_row_when_absent(db):
    rid = uuid.uuid4()
    row = await freeze_reservation_wiring(db, rid)
    assert row.reservation_id == rid
    assert row.frozen is True
    assert row.last_applied_fork_version is None


@pytest.mark.asyncio
async def test_freeze_is_idempotent(db):
    rid = uuid.uuid4()
    await freeze_reservation_wiring(db, rid)
    await freeze_reservation_wiring(db, rid)

    rows = (await db.execute(select(ReservationWiringState))).scalars().all()
    assert len(rows) == 1
    assert rows[0].frozen is True


# --- compute_backfill_assignments (migration reconstruction) ---


def _run(reservation_id, device_id, action, status, port_a, port_b, ts):
    return SimpleNamespace(
        reservation_id=reservation_id,
        device_id=device_id,
        action=action,
        status=status,
        port_a=port_a,
        port_b=port_b,
        created_at=ts,
    )


def test_backfill_reconstructs_active_from_success_connect():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    runs = [_run(rid, switch, "connect_ports", "SUCCESS", "0/0/2", "0/0/1", 1)]

    out = compute_backfill_assignments(runs)
    assert out == [
        {
            "reservation_id": rid,
            "switch_device_id": switch,
            "port_a": "0/0/1",
            "port_b": "0/0/2",
        }
    ]


def test_backfill_ignores_failed_connect():
    runs = [_run(uuid.uuid4(), uuid.uuid4(), "connect_ports", "FAILURE", "A", "B", 1)]
    assert compute_backfill_assignments(runs) == []


def test_backfill_excludes_pair_disconnected_after_connect():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    runs = [
        _run(rid, switch, "connect_ports", "SUCCESS", "A", "B", 1),
        _run(rid, switch, "disconnect_ports", "SUCCESS", "A", "B", 2),
    ]
    assert compute_backfill_assignments(runs) == []


def test_backfill_reconnect_after_disconnect_is_live():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    runs = [
        _run(rid, switch, "connect_ports", "SUCCESS", "A", "B", 1),
        _run(rid, switch, "disconnect_ports", "SUCCESS", "A", "B", 2),
        _run(rid, switch, "connect_ports", "SUCCESS", "A", "B", 3),
    ]
    out = compute_backfill_assignments(runs)
    assert len(out) == 1
    assert out[0]["port_a"], out[0]["port_b"] == ("A", "B")


def test_backfill_dedupes_to_one_row_per_pair():
    """Two SUCCESS connects on one pair yield a single reconstructed row.

    This is why a migration rerun cannot duplicate: the helper is keyed on the
    canonical (switch, pair), and the migration also skips already-ACTIVE pairs.
    """
    switch = uuid.uuid4()
    later = uuid.uuid4()
    runs = [
        _run(uuid.uuid4(), switch, "connect_ports", "SUCCESS", "A", "B", 1),
        _run(later, switch, "connect_ports", "SUCCESS", "B", "A", 2),
    ]
    out = compute_backfill_assignments(runs)
    assert len(out) == 1
    assert out[0]["reservation_id"] == later  # latest connect owns the live pair


def test_backfill_skips_run_without_reservation():
    switch = uuid.uuid4()
    runs = [_run(None, switch, "connect_ports", "SUCCESS", "A", "B", 1)]
    assert compute_backfill_assignments(runs) == []


def test_backfill_empty_runs_yields_nothing():
    assert compute_backfill_assignments([]) == []
