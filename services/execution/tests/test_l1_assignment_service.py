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
    compute_backfill_intended,
    freeze_reservation_wiring,
    pair_needs_release,
    record_l1_connect,
    record_l1_failed,
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
    assert row.intended == "ACTIVE"
    assert row.reservation_id == rid
    assert row.switch_device_id == switch
    assert (row.port_a, row.port_b) == ("0/0/1", "0/0/2")
    assert row.attempts == 0
    assert row.physical_connection_id is None
    assert row.released_at is None


@pytest.mark.asyncio
async def test_record_connect_reusable_failed_flip_sets_intended_active(db):
    """Reusing a reservation's own FAILED row (any prior intended) flips intended ACTIVE.

    Covers the case where the row was previously FAILED with intended RELEASED (a
    stuck teardown) and this reservation now wants the pair connected again: the
    reuse flip must not leave a stale RELEASED intended on an ACTIVE row.
    """
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    failed = await record_l1_failed(
        db, rid, switch, "0/0/1", "0/0/2", attempts=2, last_error="boom", intended="RELEASED"
    )
    assert failed.intended == "RELEASED"

    row = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    assert row.id == failed.id
    assert row.status == "ACTIVE"
    assert row.intended == "ACTIVE"
    assert row.last_error is None


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


def test_failed_created_at_index_covers_the_retry_sweep_query():
    """due_failed_rows filters status='FAILED' and orders by created_at (issue

    #390): the partial index must exist, cover created_at, and restrict itself
    to FAILED rows on both backends, the same way uq_l1_active_per_switch_pair
    is exercised for its own partial predicate above.
    """
    indexes = {ix.name: ix for ix in L1ConnectionAssignment.__table__.indexes}
    index = indexes["ix_l1_connection_assignments_failed_created_at"]

    assert [c.name for c in index.columns] == ["created_at"]
    assert str(index.dialect_options["sqlite"]["where"]) == "status = 'FAILED'"
    assert str(index.dialect_options["postgresql"]["where"]) == "status = 'FAILED'"


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


# --- record_l1_failed vs concurrent winners (issue #412) ---


@pytest.mark.asyncio
async def test_failed_record_never_downgrades_active_row(db):
    """A stale failure must not clobber a row a concurrent writer flipped ACTIVE.

    The issue #412 race: the in-flight apply's completion records its failure
    AFTER a manual retry already reconnected the pair. The failure is stale by
    definition (the winner proved the pair connects), so the row stays ACTIVE
    and attempts stays untouched (an ACTIVE row is immutable to failure
    writers; inflating attempts would push a healthy pair toward the retry
    cap).
    """
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    winner = await record_l1_connect(db, rid, switch, "p1", "p2")
    assert winner.status == "ACTIVE"
    attempts_before = winner.attempts

    row = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=3, last_error="stale apply failure", intended="ACTIVE"
    )
    assert row.id == winner.id
    assert row.status == "ACTIVE"
    assert row.attempts == attempts_before
    assert row.last_error != "stale apply failure"
    assert row.intended == "ACTIVE", "intended must not be mutated by the refused write"


@pytest.mark.asyncio
async def test_failed_release_direction_does_flip_an_active_row(db):
    """The #412 guard is scoped to the BUILD direction; a genuine RELEASE-direction
    failure against an ACTIVE row is not a race and must flip it FAILED.

    An ACTIVE row is the normal, expected starting state of a disconnect that
    failed (the pair is still genuinely connected): recording that as a
    retryable FAILED row with intended RELEASED is the entire point of issue
    #369, and record_l1_failed must not treat it the same as a stale connect
    failure racing a winner.
    """
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    active = await record_l1_connect(db, rid, switch, "p1", "p2")
    assert active.status == "ACTIVE"

    row = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=1, last_error="disconnect boom", intended="RELEASED"
    )
    assert row.id == active.id
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.attempts == 1
    assert row.last_error == "disconnect boom"


@pytest.mark.asyncio
async def test_failed_record_accumulates_attempts_on_failed_row(db):
    """Repeat failures upsert the same row and ACCUMULATE attempts (phase 4 cap)."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    first = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=3, last_error="boom 1", intended="ACTIVE"
    )
    second = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=1, last_error="boom 2", intended="ACTIVE"
    )
    assert second.id == first.id
    assert second.status == "FAILED"
    assert second.attempts == 4
    assert second.last_error == "boom 2"


@pytest.mark.asyncio
async def test_failed_record_creates_fresh_row_when_none_exists(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    row = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=3, last_error="boom", intended="ACTIVE"
    )
    assert row.status == "FAILED"
    assert row.attempts == 3
    assert row.last_error == "boom"
    assert row.intended == "ACTIVE"


# --- release_l1_connection ---


@pytest.mark.asyncio
async def test_release_flips_active_to_released(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")

    released = await release_l1_connection(db, rid, switch, "0/0/1", "0/0/2")
    assert released is not None
    assert released.status == "RELEASED"
    assert released.intended == "RELEASED"
    assert released.released_at is not None


@pytest.mark.asyncio
async def test_release_flips_retried_failed_row_to_released(db):
    """A retry-driven disconnect success flips a FAILED-intended-RELEASED row in
    place, the release-side mirror of record_l1_connect's reusable-FAILED flip.

    Without this broadened match, a retry that successfully disconnects a
    previously-FAILED release would leave the row stuck FAILED forever: the
    original release_l1_connection only matched status == ACTIVE.
    """
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "p1", "p2")
    failed = await record_l1_failed(
        db, rid, switch, "p1", "p2", attempts=1, last_error="boom", intended="RELEASED"
    )
    assert failed.status == "FAILED"

    released = await release_l1_connection(db, rid, switch, "p1", "p2")
    assert released is not None
    assert released.id == failed.id
    assert released.status == "RELEASED"
    assert released.intended == "RELEASED"
    assert released.last_error is None, "the resolved failure message must not linger"


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


# --- pair_needs_release (issue #369 release-direction idempotency gate) -----


@pytest.mark.asyncio
async def test_pair_needs_release_true_for_active_row(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "A", "B")
    assert await pair_needs_release(db, rid, switch, "A", "B") is True


@pytest.mark.asyncio
async def test_pair_needs_release_true_for_failed_intended_released(db):
    """A FAILED row whose intended is RELEASED is still believed live: a release
    retry must not skip it as a no-op (the bug this gate exists to prevent)."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_failed(
        db, rid, switch, "A", "B", attempts=1, last_error="boom", intended="RELEASED"
    )
    assert await pair_needs_release(db, rid, switch, "A", "B") is True


@pytest.mark.asyncio
async def test_pair_needs_release_false_for_failed_intended_active(db):
    """A FAILED row whose intended is ACTIVE (a build that never connected) has
    nothing live to release."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_failed(
        db, rid, switch, "A", "B", attempts=1, last_error="boom", intended="ACTIVE"
    )
    assert await pair_needs_release(db, rid, switch, "A", "B") is False


@pytest.mark.asyncio
async def test_pair_needs_release_false_when_no_row(db):
    assert await pair_needs_release(db, uuid.uuid4(), uuid.uuid4(), "A", "B") is False


@pytest.mark.asyncio
async def test_pair_needs_release_false_after_released(db):
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    await record_l1_connect(db, rid, switch, "A", "B")
    await release_l1_connection(db, rid, switch, "A", "B")
    assert await pair_needs_release(db, rid, switch, "A", "B") is False


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


# --- compute_backfill_intended (migration 0016 reconstruction, issue #369) ---


def _failed_row(row_id, reservation_id, switch_id, port_a, port_b):
    return SimpleNamespace(
        id=row_id,
        reservation_id=reservation_id,
        switch_device_id=switch_id,
        port_a=port_a,
        port_b=port_b,
    )


def test_backfill_intended_no_matching_run_defaults_active():
    row_id = uuid.uuid4()
    row = _failed_row(row_id, uuid.uuid4(), uuid.uuid4(), "A", "B")
    assert compute_backfill_intended([row], []) == {row_id: "ACTIVE"}


def test_backfill_intended_connect_run_yields_active():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    row_id = uuid.uuid4()
    row = _failed_row(row_id, rid, switch, "A", "B")
    runs = [_run(rid, switch, "connect_ports", "FAILED", "A", "B", 1)]
    assert compute_backfill_intended([row], runs) == {row_id: "ACTIVE"}


def test_backfill_intended_disconnect_run_yields_released():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    row_id = uuid.uuid4()
    row = _failed_row(row_id, rid, switch, "A", "B")
    # The run's own status is FAILED (that failed disconnect is WHY the row is
    # FAILED); compute_backfill_intended considers it regardless, unlike
    # compute_backfill_assignments which only trusts SUCCESS runs.
    runs = [_run(rid, switch, "disconnect_ports", "FAILED", "A", "B", 1)]
    assert compute_backfill_intended([row], runs) == {row_id: "RELEASED"}


def test_backfill_intended_uses_most_recent_run():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    row_id = uuid.uuid4()
    row = _failed_row(row_id, rid, switch, "A", "B")
    runs = [
        _run(rid, switch, "connect_ports", "SUCCESS", "A", "B", 1),
        _run(rid, switch, "disconnect_ports", "FAILED", "A", "B", 2),
    ]
    assert compute_backfill_intended([row], runs) == {row_id: "RELEASED"}


def test_backfill_intended_matches_reversed_port_order():
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    row_id = uuid.uuid4()
    # The row is stored canonical (A, B); the run recorded the reversed order.
    row = _failed_row(row_id, rid, switch, "A", "B")
    runs = [_run(rid, switch, "disconnect_ports", "FAILED", "B", "A", 1)]
    assert compute_backfill_intended([row], runs) == {row_id: "RELEASED"}


def test_backfill_intended_scoped_by_reservation():
    """A run for a DIFFERENT reservation on the same switch/pair must not leak in."""
    switch = uuid.uuid4()
    row_id = uuid.uuid4()
    row = _failed_row(row_id, uuid.uuid4(), switch, "A", "B")
    other_reservation_run = _run(uuid.uuid4(), switch, "disconnect_ports", "FAILED", "A", "B", 1)
    assert compute_backfill_intended([row], [other_reservation_run]) == {row_id: "ACTIVE"}


def test_backfill_intended_multiple_rows_independent():
    switch = uuid.uuid4()
    rid_a, rid_b = uuid.uuid4(), uuid.uuid4()
    row_a_id, row_b_id = uuid.uuid4(), uuid.uuid4()
    row_a = _failed_row(row_a_id, rid_a, switch, "A", "B")
    row_b = _failed_row(row_b_id, rid_b, switch, "C", "D")
    runs = [
        _run(rid_a, switch, "connect_ports", "FAILED", "A", "B", 1),
        _run(rid_b, switch, "disconnect_ports", "FAILED", "C", "D", 1),
    ]
    assert compute_backfill_intended([row_a, row_b], runs) == {
        row_a_id: "ACTIVE",
        row_b_id: "RELEASED",
    }


def test_backfill_intended_empty_rows_yields_nothing():
    runs = [_run(uuid.uuid4(), uuid.uuid4(), "connect_ports", "SUCCESS", "A", "B", 1)]
    assert compute_backfill_intended([], runs) == {}


# --- record-time freeze re-check and cross-reservation supersession (issue #461) ---


@pytest.mark.asyncio
async def test_record_connect_frozen_parks_failed_intended_released(db):
    """A build that lands after the wiring freeze is parked FAILED intended RELEASED
    (the record-time analogue of the #412 guard): recording it ACTIVE would strand
    live switch config the terminal teardown has already snapshotted past."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    row = await record_l1_connect(db, rid, switch, "0/0/2", "0/0/1")
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.last_error == svc.FROZEN_BUILD_PENDING_RELEASE
    assert (row.port_a, row.port_b) == ("0/0/1", "0/0/2"), "parked in canonical order"
    assert row.attempts == 0

    active = (
        (
            await db.execute(
                select(L1ConnectionAssignment).where(L1ConnectionAssignment.status == "ACTIVE")
            )
        )
        .scalars()
        .all()
    )
    assert active == [], "no ACTIVE row may exist for a frozen reservation's build"
    # The parked reason is hardware-retryable so the release channel sweeps it.
    from app.services.wiring_retry_service import is_retryable_failure

    assert is_retryable_failure(row.last_error) is True


@pytest.mark.asyncio
async def test_record_connect_frozen_reuses_failed_row_and_keeps_attempts(db):
    """The retry-tick interleaving shape: the FAILED build row that was being retried is
    parked in place (same row, intended flipped to RELEASED) and attempts do NOT
    inflate: the release direction has not failed yet."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    failed = await record_l1_failed(
        db, rid, switch, "0/0/1", "0/0/2", attempts=2, last_error="boom", intended="ACTIVE"
    )
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    row = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    assert row.id == failed.id, "the same row is parked, not a parallel row"
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.attempts == 2
    assert row.last_error == svc.FROZEN_BUILD_PENDING_RELEASE


@pytest.mark.asyncio
async def test_record_connect_frozen_short_circuits_own_active_row_unchanged(db):
    """A redelivery for a pair this reservation already holds ACTIVE stays a pure
    idempotent no-op even when frozen: no new state transition happened in this call,
    so there is nothing to park (the row predates the freeze and the teardown saw it)."""
    rid = uuid.uuid4()
    switch = uuid.uuid4()
    first = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    db.add(ReservationWiringState(reservation_id=rid, frozen=True))
    await db.commit()

    again = await record_l1_connect(db, rid, switch, "0/0/1", "0/0/2")
    assert again.id == first.id
    assert again.status == "ACTIVE"


@pytest.mark.asyncio
async def test_record_connect_supersedes_stale_cross_reservation_active_row(db):
    """Issue #461 secondary: a successful connect proves the pair, so another
    reservation's stale ACTIVE row on the same (switch, canonical pair) is flipped
    RELEASED and THIS reservation records its own ACTIVE row. The pre-#461 adoption of
    the stale row left the new reservation with no ledger row, so its own teardown
    released nothing and the leak propagated to every later booking of the pair."""
    stale_res = uuid.uuid4()
    new_res = uuid.uuid4()
    switch = uuid.uuid4()
    stale = await record_l1_connect(db, stale_res, switch, "0/0/1", "0/0/2")
    assert stale.status == "ACTIVE"

    row = await record_l1_connect(db, new_res, switch, "0/0/2", "0/0/1")
    assert row is not None
    assert row.reservation_id == new_res, "the new reservation records its OWN row"
    assert row.status == "ACTIVE"

    await db.refresh(stale)
    assert stale.status == "RELEASED"
    assert stale.intended == "RELEASED"
    assert stale.released_at is not None

    active = (
        (
            await db.execute(
                select(L1ConnectionAssignment).where(L1ConnectionAssignment.status == "ACTIVE")
            )
        )
        .scalars()
        .all()
    )
    assert [r.reservation_id for r in active] == [new_res], "exactly one ACTIVE row remains"
