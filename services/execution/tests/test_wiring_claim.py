"""Tests for the per-row wiring drive claim (issue #817).

The two retry channels (the manual endpoint's reattempt_reservation and the
background run_wiring_retry_tick) could load the same FAILED ledger row and each
call the driver for it. Issue #814 made the failure WRITE a row-identity
compare-and-swap, so the ledger could not be corrupted, but the duplicate driver
call was still reachable. The claim closes it: a `claimed_until` stamp taken by a
compare-and-swap immediately before each retried row's own driver call.

Covered here, at all three layers against in-memory SQLite:

  - the claim budget's derivation from the existing driver-timeout values,
  - a claimed row is invisible to the other channel's per-tick select, and an
    expired claim is reclaimable,
  - the claim compare-and-swap itself: a second claimant loses, a row that is no
    longer FAILED cannot be claimed at all,
  - every record path clears the stamp (success, the row-identity failure write,
    the key upsert, release, and the stale-intent park),
  - the manual channel reports `in_progress`, not `still_failed`, for a row the
    tick already holds, and drives nothing for it,
  - the L1 and L2 FAILED-to-ACTIVE success flips return the WINNER's row when
    their compare-and-swap finds rowcount zero (the alignment with L3's
    record_route_active).

SQLite ignores FOR UPDATE, so nothing here proves the SKIP LOCKED half of the
selects; the conditional UPDATE is the real race guard on every dialect and that
is what these tests pin. The genuinely concurrent, two-session proof is
tests/test_wiring_retry_claim_race_live_pg.py, which runs both channels as real
tasks against a live Postgres.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import l1_assignment_service as l1svc
from app.services import l2_membership_service as l2svc
from app.services.l1_assignment_service import (
    due_failed_rows,
    park_stale_l1_build,
    record_l1_connect,
    record_l1_failed,
    release_l1_connection,
)
from app.services.l2_membership_service import (
    due_failed_l2_rows,
    park_stale_l2_build,
    record_l2_failed,
    record_l2_membership_active,
    release_l2_membership,
)
from app.services.route_service import (
    due_failed_route_rows,
    park_stale_route_build,
    record_route_active,
    record_route_failed,
    release_route_membership,
)
from app.services.wiring_claim import (
    WiringRowClaims,
    claim_budget,
    claimable_row_ids,
)
from app.services.wiring_retry_service import reattempt_reservation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

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


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


RES_ID = uuid.uuid4()
SWITCH_ID = uuid.uuid4()


async def _seed_l1_failed(
    port_a="0/0/1",
    port_b="0/0/2",
    intended="ACTIVE",
    attempts=0,
    claimed_until=None,
    reservation_id=None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id or RES_ID,
            switch_device_id=SWITCH_ID,
            port_a=port_a,
            port_b=port_b,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error="driver timeout",
            claimed_until=claimed_until,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _seed_alloc() -> uuid.UUID:
    async with TestSessionLocal() as db:
        va = VlanAssignment(
            reservation_id=RES_ID,
            fabric_id=uuid.uuid4(),
            vlan_id=100,
        )
        db.add(va)
        await db.commit()
        return va.id


async def _seed_l2_failed(
    va_id,
    port="0/0/1",
    intended="ACTIVE",
    attempts=0,
    claimed_until=None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        row = L2PortAssignment(
            reservation_id=RES_ID,
            vlan_assignment_id=va_id,
            switch_device_id=SWITCH_ID,
            port=port,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error="driver timeout",
            claimed_until=claimed_until,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _seed_l3_failed(
    intended="ACTIVE",
    attempts=0,
    claimed_until=None,
    device_id=None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        row = RouteAssignment(
            reservation_id=RES_ID,
            device_id=device_id or SWITCH_ID,
            routes=[{"destination": "10.0.0.0/24", "next_hop": "10.0.0.1"}],
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error="driver timeout",
            claimed_until=claimed_until,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _claimed_until(model, row_id):
    async with TestSessionLocal() as db:
        row = (await db.execute(select(model).where(model.id == row_id))).scalar_one()
        return row.claimed_until


def _future(seconds=600):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds=600):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# --- The budget derivation ---------------------------------------------------


def test_claim_budget_covers_one_row_worst_case_drive():
    """3 attempts x 3 actions (login, op, logout) x the 30s driver timeout, plus the
    in-line backoff, rounded up to a whole minute: 5 minutes at the defaults."""
    from app.services.nats_consumer import WIRING_DRIVER_ATTEMPTS

    assert settings.execution_timeout_seconds == 30
    assert WIRING_DRIVER_ATTEMPTS == 3
    assert claim_budget() == timedelta(minutes=5)


def test_claim_budget_scales_with_the_driver_timeout():
    """A deployment that raises the driver timeout gets a longer budget for free: the
    point of deriving it from execution_timeout_seconds instead of pinning a knob."""
    with patch.object(settings, "execution_timeout_seconds", 120):
        # 3 attempts x 3 actions x 120s = 1080s, plus 5.4s of backoff, rounded up.
        assert claim_budget() == timedelta(minutes=19)
    with patch.object(settings, "execution_timeout_seconds", 1):
        # Never degenerate: the floor is one whole minute.
        assert claim_budget() >= timedelta(minutes=1)


# --- A claimed row is invisible to the other channel's per-tick select --------


async def test_l1_claimed_row_is_invisible_to_the_tick_select(db):
    await _seed_l1_failed(claimed_until=_future())
    assert await due_failed_rows(db, 10, 5) == []


async def test_l1_expired_claim_is_reclaimable(db):
    row_id = await _seed_l1_failed(claimed_until=_past())
    due = await due_failed_rows(db, 10, 5)
    assert [r.id for r in due] == [row_id], "an expired claim must not park a row forever"


async def test_l1_unclaimed_row_is_selected(db):
    row_id = await _seed_l1_failed()
    assert [r.id for r in await due_failed_rows(db, 10, 5)] == [row_id]


async def test_l2_claimed_row_is_invisible_and_expiry_reclaims(db):
    va = await _seed_alloc()
    held = await _seed_l2_failed(va, port="0/0/1", claimed_until=_future())
    expired = await _seed_l2_failed(va, port="0/0/2", claimed_until=_past())
    ids = {r.id for r in await due_failed_l2_rows(db, 10, 5)}
    assert held not in ids
    assert expired in ids


async def test_l3_claimed_row_is_invisible_and_expiry_reclaims(db):
    held = await _seed_l3_failed(claimed_until=_future(), device_id=uuid.uuid4())
    expired = await _seed_l3_failed(claimed_until=_past(), device_id=uuid.uuid4())
    ids = {r.id for r in await due_failed_route_rows(db, 10, 5)}
    assert held not in ids
    assert expired in ids


# --- The claim compare-and-swap itself ---------------------------------------


async def test_claim_stamps_the_budget_and_a_second_claimant_loses(db):
    row_id = await _seed_l1_failed()
    first = WiringRowClaims()
    second = WiringRowClaims()

    assert await first.claim(db, L1ConnectionAssignment, row_id) is True
    stamp = await _claimed_until(L1ConnectionAssignment, row_id)
    assert stamp is not None

    async with TestSessionLocal() as other:
        assert await second.claim(other, L1ConnectionAssignment, row_id) is False
    assert second.lost == {row_id}
    assert first.held == {row_id}
    # The loser must not move the winner's stamp.
    assert await _claimed_until(L1ConnectionAssignment, row_id) == stamp


async def test_claim_is_idempotent_within_one_pass(db):
    """A pass that reaches the same row twice keeps its own claim rather than losing
    it to itself."""
    row_id = await _seed_l1_failed()
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id) is True
    assert await claims.claim(db, L1ConnectionAssignment, row_id) is True
    assert claims.lost == set()


async def test_claim_of_none_row_id_is_always_granted(db):
    """The reconcile path has no row identity to claim and is never blocked."""
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, None) is True
    assert claims.held == set()


async def test_a_row_no_longer_failed_cannot_be_claimed(db):
    """The compare-and-swap is on (id, status FAILED): a row a concurrent writer
    already flipped ACTIVE is not claimable, so no driver call is spent on it."""
    row_id = await _seed_l1_failed()
    async with TestSessionLocal() as s:
        row = await s.get(L1ConnectionAssignment, row_id)
        row.status = "ACTIVE"
        await s.commit()
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id) is False
    assert claims.lost == {row_id}


async def test_claim_expiry_lets_a_new_pass_take_a_dead_holders_row(db):
    """No reaper and no heartbeat: a process that died mid-drive leaves a stamp that
    a later pass simply outlives."""
    row_id = await _seed_l1_failed(claimed_until=_past())
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id) is True


async def test_claimable_row_ids_answers_only_the_free_rows(db):
    free = await _seed_l1_failed(port_a="0/0/1", port_b="0/0/2")
    held = await _seed_l1_failed(port_a="0/0/3", port_b="0/0/4", claimed_until=_future())
    expired = await _seed_l1_failed(port_a="0/0/5", port_b="0/0/6", claimed_until=_past())
    answer = await claimable_row_ids(db, L1ConnectionAssignment, [free, held, expired])
    assert answer == {free, expired}


async def test_claimable_row_ids_of_nothing_is_empty(db):
    assert await claimable_row_ids(db, L1ConnectionAssignment, []) == set()


# --- Every record path clears the stamp --------------------------------------


async def test_l1_success_flip_clears_the_claim(db):
    row_id = await _seed_l1_failed(claimed_until=_future())
    await record_l1_connect(db, RES_ID, SWITCH_ID, "0/0/1", "0/0/2")
    assert await _claimed_until(L1ConnectionAssignment, row_id) is None


async def test_l1_release_clears_the_claim(db):
    row_id = await _seed_l1_failed(intended="RELEASED", claimed_until=_future())
    await release_l1_connection(db, RES_ID, SWITCH_ID, "0/0/1", "0/0/2")
    assert await _claimed_until(L1ConnectionAssignment, row_id) is None


async def test_l1_row_identity_failure_write_clears_the_claim(db):
    row_id = await _seed_l1_failed(claimed_until=_future())
    await record_l1_failed(
        db, RES_ID, SWITCH_ID, "0/0/1", "0/0/2", 1, "again", intended="ACTIVE", row_id=row_id
    )
    assert await _claimed_until(L1ConnectionAssignment, row_id) is None


async def test_l1_key_upsert_failure_write_clears_the_claim(db):
    row_id = await _seed_l1_failed(claimed_until=_future())
    await record_l1_failed(db, RES_ID, SWITCH_ID, "0/0/1", "0/0/2", 1, "again", intended="ACTIVE")
    assert await _claimed_until(L1ConnectionAssignment, row_id) is None


async def test_l1_stale_build_park_clears_the_claim(db):
    row_id = await _seed_l1_failed(claimed_until=_future())
    await park_stale_l1_build(db, row_id, "intent gone")
    assert await _claimed_until(L1ConnectionAssignment, row_id) is None


async def test_l2_success_flip_clears_the_claim(db):
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va, claimed_until=_future())
    await record_l2_membership_active(db, RES_ID, va, SWITCH_ID, "0/0/1")
    assert await _claimed_until(L2PortAssignment, row_id) is None


async def test_l2_release_clears_the_claim(db):
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va, intended="RELEASED", claimed_until=_future())
    await release_l2_membership(db, RES_ID, SWITCH_ID, "0/0/1")
    assert await _claimed_until(L2PortAssignment, row_id) is None


async def test_l2_row_identity_failure_write_clears_the_claim(db):
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va, claimed_until=_future())
    await record_l2_failed(
        db, RES_ID, va, SWITCH_ID, "0/0/1", 1, "again", intended="ACTIVE", row_id=row_id
    )
    assert await _claimed_until(L2PortAssignment, row_id) is None


async def test_l2_key_upsert_failure_write_clears_the_claim(db):
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va, claimed_until=_future())
    await record_l2_failed(db, RES_ID, va, SWITCH_ID, "0/0/1", 1, "again", intended="ACTIVE")
    assert await _claimed_until(L2PortAssignment, row_id) is None


async def test_l2_stale_build_park_clears_the_claim(db):
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va, claimed_until=_future())
    await park_stale_l2_build(db, row_id, "intent gone")
    assert await _claimed_until(L2PortAssignment, row_id) is None


async def test_l3_success_flip_clears_the_claim(db):
    row_id = await _seed_l3_failed(claimed_until=_future())
    await record_route_active(db, RES_ID, SWITCH_ID, [{"destination": "10.0.0.0/24"}])
    assert await _claimed_until(RouteAssignment, row_id) is None


async def test_l3_release_clears_the_claim(db):
    row_id = await _seed_l3_failed(intended="RELEASED", claimed_until=_future())
    await release_route_membership(db, RES_ID, SWITCH_ID)
    assert await _claimed_until(RouteAssignment, row_id) is None


async def test_l3_row_identity_failure_write_clears_the_claim(db):
    row_id = await _seed_l3_failed(claimed_until=_future())
    await record_route_failed(
        db, RES_ID, SWITCH_ID, None, 1, "again", intended="ACTIVE", row_id=row_id
    )
    assert await _claimed_until(RouteAssignment, row_id) is None


async def test_l3_key_upsert_failure_write_clears_the_claim(db):
    row_id = await _seed_l3_failed(claimed_until=_future())
    await record_route_failed(db, RES_ID, SWITCH_ID, None, 1, "again", intended="ACTIVE")
    assert await _claimed_until(RouteAssignment, row_id) is None


async def test_l3_stale_build_park_clears_the_claim(db):
    row_id = await _seed_l3_failed(claimed_until=_future())
    await park_stale_route_build(db, row_id, "intent gone")
    assert await _claimed_until(RouteAssignment, row_id) is None


# --- The manual channel reports in_progress ----------------------------------


async def test_manual_retry_reports_in_progress_for_a_row_the_tick_holds():
    """The claimed row is reported, not omitted: an absent row would read to the
    caller as one that was already fixed. No driver machinery is patched here
    because a fully-claimed retryable set never reaches the apply at all."""
    row_id = await _seed_l1_failed(claimed_until=_future())
    result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert [r["outcome"] for r in result["results"]] == ["in_progress"]
    assert result["results"][0]["id"] == str(row_id)
    # The stamp is the other channel's; this call must not touch it.
    assert await _claimed_until(L1ConnectionAssignment, row_id) is not None


async def test_manual_retry_reports_in_progress_at_all_three_layers():
    va = await _seed_alloc()
    l1 = await _seed_l1_failed(claimed_until=_future())
    l2 = await _seed_l2_failed(va, claimed_until=_future())
    l3 = await _seed_l3_failed(claimed_until=_future())
    result = await reattempt_reservation(RES_ID, _db_session_factory())
    by_id = {r["id"]: r for r in result["results"]}
    assert by_id[str(l1)]["outcome"] == "in_progress"
    assert by_id[str(l1)]["layer"] == "l1"
    assert by_id[str(l2)]["outcome"] == "in_progress"
    assert by_id[str(l2)]["layer"] == "l2"
    assert by_id[str(l3)]["outcome"] == "in_progress"
    assert by_id[str(l3)]["layer"] == "l3"


async def test_manual_retry_still_reports_not_retryable_over_in_progress():
    """A pinned-reason row is classified before the claim is ever consulted: its
    recovery is a re-save either way, and it never burns a driver call."""
    from app.services.nats_consumer import WIRING_UNRESOLVABLE_REASON

    row_id = await _seed_l1_failed(claimed_until=_future())
    async with TestSessionLocal() as s:
        row = await s.get(L1ConnectionAssignment, row_id)
        row.last_error = f"{WIRING_UNRESOLVABLE_REASON}: switch gone"
        await s.commit()
    result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert [r["outcome"] for r in result["results"]] == ["not_retryable"]


# --- The L1/L2 success flips return the winner on rowcount zero --------------


async def test_l1_success_flip_returns_the_winner_when_its_cas_loses(db):
    """record_l1_connect's FAILED-to-ACTIVE flip is now the same SQL compare-and-swap
    record_route_active uses: a concurrent writer that released the row between the
    SELECT and the UPDATE keeps its result, and the loser returns the winner's row."""
    row_id = await _seed_l1_failed()
    async with TestSessionLocal() as s:
        stale = await s.get(L1ConnectionAssignment, row_id)
    # The winner releases the row after our SELECT would have read it as FAILED.
    async with TestSessionLocal() as s:
        row = await s.get(L1ConnectionAssignment, row_id)
        row.status = "RELEASED"
        row.intended = "RELEASED"
        await s.commit()

    with patch.object(l1svc, "_find_reusable_failed", AsyncMock(return_value=stale)):
        returned = await record_l1_connect(db, RES_ID, SWITCH_ID, "0/0/1", "0/0/2")

    assert returned is not None
    assert returned.id == row_id
    assert returned.status == "RELEASED", "the winner's status must not be overwritten"
    async with TestSessionLocal() as s:
        fresh = await s.get(L1ConnectionAssignment, row_id)
        assert fresh.status == "RELEASED"


async def test_l2_success_flip_returns_the_winner_when_its_cas_loses(db):
    """The record_l2_membership_active mirror of the L1 case above."""
    va = await _seed_alloc()
    row_id = await _seed_l2_failed(va)
    async with TestSessionLocal() as s:
        stale = await s.get(L2PortAssignment, row_id)
    async with TestSessionLocal() as s:
        row = await s.get(L2PortAssignment, row_id)
        row.status = "RELEASED"
        row.intended = "RELEASED"
        await s.commit()

    with patch.object(l2svc, "_find_reusable_failed", AsyncMock(return_value=stale)):
        returned = await record_l2_membership_active(db, RES_ID, va, SWITCH_ID, "0/0/1")

    assert returned is not None
    assert returned.id == row_id
    assert returned.status == "RELEASED", "the winner's status must not be overwritten"
    async with TestSessionLocal() as s:
        fresh = await s.get(L2PortAssignment, row_id)
        assert fresh.status == "RELEASED"
