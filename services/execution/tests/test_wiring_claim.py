"""Tests for the per-row wiring drive claim (issue #817).

The two retry channels (the manual endpoint's reattempt_reservation and the
background run_wiring_retry_tick) could load the same FAILED ledger row and each
call the driver for it. Issue #814 made the failure WRITE a row-identity
compare-and-swap, so the ledger could not be corrupted, but the duplicate driver
call was still reachable. The claim closes it: a `claimed_until` stamp taken by a
compare-and-swap immediately before each retried row's own driver call.

Covered here, at all three layers against in-memory SQLite:

  - the claim budget's derivation from the existing driver-timeout values, and an
    L3 pin's claim sized for its whole drive (issue #1482): on a virtual clock, every
    driver action at its worst case, the claim is still held when the logout ends
    (sampled at 0, 1, 2, 10, and 200 route operations through the retry pass and a
    provision of 4 through the apply; with 999 ms of overhead per action, at every
    count from 0 to 300), a far larger overhead is a pinned known limit, and a sized
    claim still expires at its stamp,
  - a claimed row is invisible to the other channel's per-tick select, and an
    expired claim is reclaimable,
  - the claim compare-and-swap itself: a second claimant loses, a row that is no
    longer FAILED cannot be claimed at all,
  - every record path clears the stamp (success, the row-identity failure write,
    the key upsert, release, and the stale-intent park),
  - the row-identity failure write respects the claim (issue #1479): it writes on a
    row the calling pass holds, an unclaimed row, or an expired claim, and on a row
    another pass holds it changes nothing and the pass reports `in_progress`,
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
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
    L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS,
    ROW_DRIVER_ACTIONS,
    WiringRowClaims,
    claim_budget,
    claimable_row_ids,
    l3_pin_claim_budget,
    write_guard,
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
    routes=None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        row = RouteAssignment(
            reservation_id=RES_ID,
            device_id=device_id or SWITCH_ID,
            routes=routes
            if routes is not None
            else [{"destination": "10.0.0.0/24", "next_hop": "10.0.0.1"}],
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


async def _true_direction(model, row_id) -> str:
    """The direction the row intends now: what a pass that wants to drive it passes to
    `claim` (issue #1496), so a refusal in a test means the claim is held, never a
    direction mismatch."""
    async with TestSessionLocal() as db:
        return (await db.execute(select(model.intended).where(model.id == row_id))).scalar_one()


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
        # 3 attempts x 3 actions x 120s = 1080s, plus 3 x 0.6s = 1.8s of backoff,
        # rounded up.
        assert claim_budget() == timedelta(minutes=19)
    with patch.object(settings, "execution_timeout_seconds", 1):
        # Never degenerate: the floor is one whole minute.
        assert claim_budget() >= timedelta(minutes=1)


def _worst_case_action_ms(timeout: int) -> int:
    """One driver action's worst case in milliseconds, computed here rather than by
    the code under test: three attempts each running to the timeout, plus the 0.2s
    and 0.4s backoff sleeps between them."""
    from app.services.nats_consumer import (
        WIRING_DRIVER_ATTEMPTS,
        WIRING_DRIVER_BACKOFF_FACTOR,
        WIRING_DRIVER_INITIAL_DELAY,
        WIRING_DRIVER_MAX_DELAY,
    )

    assert (
        WIRING_DRIVER_ATTEMPTS,
        WIRING_DRIVER_INITIAL_DELAY,
        WIRING_DRIVER_BACKOFF_FACTOR,
        WIRING_DRIVER_MAX_DELAY,
    ) == (3, 0.2, 2.0, 2.0), "the literal arithmetic below assumes these constants"
    return 3 * timeout * 1000 + 200 + 400


def test_the_one_row_budget_is_unchanged_for_l1_and_l2():
    """Issue #1482 must not move the L1 and L2 budget: the default is still three
    actions (login, op, logout), 5 minutes at the default timeout and 19 at 120s,
    and for every whole-second timeout up to an hour it is the plain round-up of
    3 x (3 x timeout + 0.6s) it was before."""
    assert ROW_DRIVER_ACTIONS == 3
    assert claim_budget() == claim_budget(3) == timedelta(minutes=5)
    assert WiringRowClaims().budget == timedelta(minutes=5)
    with patch.object(settings, "execution_timeout_seconds", 120):
        assert claim_budget() == timedelta(minutes=19)
        assert WiringRowClaims().budget == timedelta(minutes=19)
    slacks = []
    for timeout in range(1, 3601):
        with patch.object(settings, "execution_timeout_seconds", timeout):
            before = -(-(3 * _worst_case_action_ms(timeout)) // 60_000)  # ceil
            assert claim_budget() == timedelta(minutes=before), timeout
            slacks.append(before * 60_000 - 3 * _worst_case_action_ms(timeout))
    # The slack WIRE-CLAIM-3 states for one row: never under 1.2s.
    assert min(slacks) == 1200


# The per-action allowance for time outside the sandbox timers, added to an L3 pin's
# budget only (issue #1482); a literal here, so a change to the constant is seen.
ALLOWANCE_MS = 1000


# The dependency install's own timeout (issue #1495); a literal here, so a change to
# the constant is seen.
INSTALL_MS = 60_000


def test_with_pip_install_on_the_budget_counts_one_install_per_action():
    """Issue #1495: with ALLOW_DRIVER_PIP_INSTALL on, each action's worst case is one
    60s install (once per action, never per attempt) plus its three attempts and its
    backoff. One row: 3 x 150.6s = 451.8s, 8 minutes; an L2 add (four actions):
    602.4s, 11 minutes; with the flag off, unchanged. For every whole-second timeout
    up to an hour the budget is the plain round-up of that sum, with the same 1.2s
    of least slack as without the install (60s per action is a whole minute per row
    of three)."""
    from app.services.wiring_claim import install_ms_per_action

    assert install_ms_per_action() == 0
    assert claim_budget() == timedelta(minutes=5)
    with patch.object(settings, "allow_driver_pip_install", True):
        assert install_ms_per_action() == INSTALL_MS
        assert claim_budget() == timedelta(minutes=8)
        assert claim_budget(4) == timedelta(minutes=11)
        assert WiringRowClaims().budget == timedelta(minutes=8)
        slacks = []
        for timeout in range(1, 3601):
            with patch.object(settings, "execution_timeout_seconds", timeout):
                drive = 3 * (INSTALL_MS + _worst_case_action_ms(timeout))
                minutes = -(-drive // 60_000)  # ceil
                assert claim_budget() == timedelta(minutes=minutes), timeout
                slacks.append(minutes * 60_000 - drive)
        assert min(slacks) == 1200


@pytest.mark.parametrize(
    ("route_operations", "minutes"),
    [
        # (R + 2) x (60s + 90.6s + the 1s allowance), never under the one-row 8 minutes.
        (0, 8),  # 303.2s, held at the one-row 8 minutes (451.8s with the install)
        (1, 8),  # 454.8s
        (2, 11),  # 606.4s
        (10, 31),  # 1819.2s
        (1_000, 2_532),  # 151,903.2s
    ],
)
def test_with_pip_install_on_an_l3_pins_budget_counts_one_install_per_action(
    route_operations, minutes
):
    """The L3 pin's budget takes the install into every one of its R + 2 actions."""
    with patch.object(settings, "allow_driver_pip_install", True):
        assert l3_pin_claim_budget(route_operations) == timedelta(minutes=minutes)
        budget = l3_pin_claim_budget(route_operations) // timedelta(milliseconds=1)
        drive = (route_operations + 2) * (INSTALL_MS + _worst_case_action_ms(30) + ALLOWANCE_MS)
        assert budget > drive


@pytest.mark.parametrize(
    ("route_operations", "timeout", "minutes"),
    [
        # (R + 2) x (90.6s + the 1s allowance) at the default timeout, never under the
        # 3-action 5 minutes.
        (0, 30, 5),  # login, logout: 183.2s, held at the one-row 5 minutes
        (1, 30, 5),  # 274.8s
        (2, 30, 7),  # 366.4s, the issue's first overrun of 300s
        (3, 30, 8),  # 458.0s
        (10, 30, 19),  # 1099.2s, the issue's second example
        (47, 30, 75),  # 4488.4s
        # 131 actions are 11,999.6s: the smallest slack at this timeout, 0.4s.
        (129, 30, 200),
        # 150 actions are 13,740s, exactly 229 minutes: a stamp at the drive's end
        # would be reclaimable at that instant, so the budget is the minute after.
        (148, 30, 230),
        (200, 30, 309),  # 18,503.2s
        (1_000, 30, 1_530),  # the per-switch route limit (#1493): about 25.5 hours
        # A retry provision removing a residue of 1,000 and configuring a disjoint
        # 1,000 (reconcile deltas take no claim).
        (2_000, 30, 3_057),
        # 4,580,183.2s, about 53 days: the arithmetic only; since #1493 only unlimited
        # removals (a FAILED pin's residue, or a deprovision) can reach a count like this.
        (50_000, 30, 76_337),
        # The same arithmetic at a raised timeout: (R + 2) x 361.6s.
        (0, 120, 19),
        (2, 120, 25),  # 1446.4s
        (10, 120, 73),  # 4339.2s
    ],
)
def test_l3_pin_claim_budget_is_r_plus_two_actions_rounded_up(route_operations, timeout, minutes):
    """An L3 pin's one claim covers the login, every route operation, and the logout:
    (R + 2) worst-case actions plus the per-action allowance, rounded up to the next
    whole minute, never under the one-row budget (issue #1482)."""
    assert L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS == ALLOWANCE_MS
    with patch.object(settings, "execution_timeout_seconds", timeout):
        assert l3_pin_claim_budget(route_operations) == timedelta(minutes=minutes)


@pytest.mark.parametrize("timeout", [1, 30, 120])
def test_l3_pin_claim_budget_outlives_every_drive_and_by_at_most_a_minute(timeout):
    """The invariant, enumerated: for every route-operation count R from 0 to 300 and
    a few far larger ones, the budget is strictly longer than the worst case of the
    whole drive (R + 2 actions) plus the allowance for each action, never under the
    one-row budget, and at most one minute longer than that sum or the one-row worst
    case, whichever is longer: the bound on how long a dead holder can keep a pin."""
    per_action = _worst_case_action_ms(timeout)
    with patch.object(settings, "execution_timeout_seconds", timeout):
        one_row = claim_budget() // timedelta(milliseconds=1)
        for route_operations in [*range(301), 1_000, 10_000, 50_000]:
            budget = l3_pin_claim_budget(route_operations) // timedelta(milliseconds=1)
            drive = (route_operations + 2) * (per_action + ALLOWANCE_MS)
            assert budget > drive, route_operations
            assert budget >= one_row, route_operations
            assert budget <= max(drive, 3 * per_action) + 60_000, route_operations


def test_the_route_limit_puts_a_number_on_the_longest_claim_at_the_defaults():
    """Issue #1493: one provision configures at most MAX_ROUTES_PER_SWITCH routes, so a
    retry provision that removes nothing holds its claim at most 1,530 minutes at the
    default timeout, and one that removes a residue of 1,000 and configures a disjoint
    1,000 at most 3,057 minutes (WIRE-CLAIM-3; reconcile deltas take no claim). Tied to
    the shared constant, so a new limit fails here until the stated numbers move with
    it."""
    from herd_common.l3_route_limit import MAX_ROUTES_PER_SWITCH

    with patch.object(settings, "execution_timeout_seconds", 30):
        assert l3_pin_claim_budget(MAX_ROUTES_PER_SWITCH) == timedelta(minutes=1_530)
        assert l3_pin_claim_budget(2 * MAX_ROUTES_PER_SWITCH) == timedelta(minutes=3_057)


def test_the_smallest_l3_slack_at_the_default_timeout_is_four_tenths_of_a_second():
    """The rounding slack left over the drive plus the allowance, at the default
    timeout, over every R up to 30,000: never under 0.4s, reached first at R = 129
    and then every 150 route operations, as WIRE-CLAIM-3 states."""
    per_action = _worst_case_action_ms(30) + ALLOWANCE_MS
    slack = {
        r: l3_pin_claim_budget(r) // timedelta(milliseconds=1) - (r + 2) * per_action
        for r in range(30_001)
    }
    smallest = min(slack.values())
    assert smallest == 400
    assert [r for r, ms in slack.items() if ms == smallest][:3] == [129, 279, 429]


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

    assert await first.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is True
    stamp = await _claimed_until(L1ConnectionAssignment, row_id)
    assert stamp is not None

    async with TestSessionLocal() as other:
        assert await second.claim(other, L1ConnectionAssignment, row_id, intended="ACTIVE") is False
    assert second.lost == {row_id}
    assert set(first.held) == {row_id}
    # The loser must not move the winner's stamp.
    assert await _claimed_until(L1ConnectionAssignment, row_id) == stamp


async def test_claim_is_idempotent_within_one_pass(db):
    """A pass that reaches the same row twice keeps its own claim rather than losing
    it to itself."""
    row_id = await _seed_l1_failed()
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is True
    assert await claims.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is True
    assert claims.lost == set()


async def test_claim_of_none_row_id_is_always_granted(db):
    """The reconcile path has no row identity to claim and is never blocked."""
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, None, intended="ACTIVE") is True
    assert claims.held == {}


async def test_a_row_no_longer_failed_cannot_be_claimed(db):
    """The compare-and-swap is on (id, status FAILED): a row a concurrent writer
    already flipped ACTIVE is not claimable, so no driver call is spent on it."""
    row_id = await _seed_l1_failed()
    async with TestSessionLocal() as s:
        row = await s.get(L1ConnectionAssignment, row_id)
        row.status = "ACTIVE"
        await s.commit()
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is False
    assert claims.lost == {row_id}


async def test_claim_expiry_lets_a_new_pass_take_a_dead_holders_row(db):
    """No reaper and no heartbeat: a process that died mid-drive leaves a stamp that
    a later pass simply outlives."""
    row_id = await _seed_l1_failed(claimed_until=_past())
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is True


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
    """The holder's own failure write ends its drive and clears its stamp. Since
    issue #1479 the holder says it holds the row by passing its claim ledger."""
    row_id = await _seed_l1_failed()
    claims = WiringRowClaims()
    assert await claims.claim(db, L1ConnectionAssignment, row_id, intended="ACTIVE") is True
    await record_l1_failed(
        db,
        RES_ID,
        SWITCH_ID,
        "0/0/1",
        "0/0/2",
        1,
        "again",
        intended="ACTIVE",
        row_id=row_id,
        claims=claims,
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
    row_id = await _seed_l2_failed(va)
    claims = WiringRowClaims()
    assert await claims.claim(db, L2PortAssignment, row_id, intended="ACTIVE") is True
    await record_l2_failed(
        db,
        RES_ID,
        va,
        SWITCH_ID,
        "0/0/1",
        1,
        "again",
        intended="ACTIVE",
        row_id=row_id,
        claims=claims,
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
    row_id = await _seed_l3_failed()
    claims = WiringRowClaims()
    assert await claims.claim(db, RouteAssignment, row_id, intended="ACTIVE") is True
    await record_route_failed(
        db, RES_ID, SWITCH_ID, None, 1, "again", intended="ACTIVE", row_id=row_id, claims=claims
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


# --- A retry failure write respects another pass's claim (issue #1479) --------
#
# The row-identity failure write is a compare-and-swap on (id, status FAILED) plus
# the claim clause: it writes on a row the calling pass holds (its ledger's `held`)
# or on a row nobody holds (never claimed, or the claim expired). On a row another
# pass holds it writes nothing at all and the row joins the caller's `lost`, which
# the pass reports `in_progress`.

LAYERS = ("l1", "l2", "l3")
_MODEL = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}


async def _seed_layer(layer: str, claimed_until=None, intended="ACTIVE") -> uuid.UUID:
    if layer == "l1":
        return await _seed_l1_failed(intended=intended, claimed_until=claimed_until)
    if layer == "l2":
        va = await _seed_alloc()
        return await _seed_l2_failed(va, intended=intended, claimed_until=claimed_until)
    return await _seed_l3_failed(intended=intended, claimed_until=claimed_until)


async def _write_failure(layer: str, db, row_id, claims):
    """The layer's retry-path failure write: 2 attempts, reason "again"."""
    if layer == "l1":
        return await record_l1_failed(
            db,
            RES_ID,
            SWITCH_ID,
            "0/0/1",
            "0/0/2",
            2,
            "again",
            intended="ACTIVE",
            row_id=row_id,
            claims=claims,
        )
    if layer == "l2":
        return await record_l2_failed(
            db,
            RES_ID,
            None,
            SWITCH_ID,
            "0/0/1",
            2,
            "again",
            intended="ACTIVE",
            row_id=row_id,
            claims=claims,
        )
    return await record_route_failed(
        db, RES_ID, SWITCH_ID, None, 2, "again", intended="ACTIVE", row_id=row_id, claims=claims
    )


async def _ledger_state(layer: str, row_id):
    model = _MODEL[layer]
    async with TestSessionLocal() as s:
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        return row.status, row.attempts, row.last_error, row.claimed_until


@pytest.mark.parametrize("layer", LAYERS)
async def test_failure_write_on_a_row_this_pass_holds_is_written(db, layer):
    row_id = await _seed_layer(layer)
    mine = WiringRowClaims()
    assert await mine.claim(db, _MODEL[layer], row_id, intended="ACTIVE") is True

    assert await _write_failure(layer, db, row_id, mine) is not None
    assert await _ledger_state(layer, row_id) == ("FAILED", 2, "again", None)
    assert mine.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
async def test_failure_write_on_a_row_another_pass_holds_changes_nothing(db, layer):
    """The issue #1479 defect: this write used to overwrite last_error, add attempts,
    and clear the holder's stamp while the holder was still driving the row."""
    row_id = await _seed_layer(layer)
    other = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await other.claim(s, _MODEL[layer], row_id, intended="ACTIVE") is True
    before = await _ledger_state(layer, row_id)
    assert before[3] is not None

    mine = WiringRowClaims()
    assert await _write_failure(layer, db, row_id, mine) is None
    assert await _ledger_state(layer, row_id) == before, "a refused write changes nothing"
    assert mine.lost == {row_id}, "the refused row is reported in_progress by the pass"

    # A caller with no ledger at all holds nothing, so it is refused the same way.
    assert await _write_failure(layer, db, row_id, None) is None
    assert await _ledger_state(layer, row_id) == before


@pytest.mark.parametrize("layer", LAYERS)
async def test_failure_write_on_an_unclaimed_row_is_written(db, layer):
    row_id = await _seed_layer(layer)
    mine = WiringRowClaims()
    assert await _write_failure(layer, db, row_id, mine) is not None
    assert await _ledger_state(layer, row_id) == ("FAILED", 2, "again", None)
    assert mine.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
async def test_failure_write_on_an_expired_claim_is_written(db, layer):
    """A dead holder's stale stamp blocks nothing, the same expiry rule as the claim."""
    row_id = await _seed_layer(layer, claimed_until=_past())
    mine = WiringRowClaims()
    assert await _write_failure(layer, db, row_id, mine) is not None
    assert await _ledger_state(layer, row_id) == ("FAILED", 2, "again", None)
    assert mine.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_pass_whose_switch_load_fails_reports_a_held_row_in_progress(layer):
    """Pass level: a retry pass handed a row it loaded before another pass claimed it
    cannot load the switch (inventory 404). Its failure write is refused, the row the
    holder is driving keeps its reason, attempts, and stamp, and the pass reports the
    row `in_progress`, not `still_failed`."""
    from app.services.wiring_retry_service import (
        _reattempt_l2_rows,
        _reattempt_l3_rows,
        _reattempt_rows,
    )

    row_id = await _seed_layer(layer, intended="RELEASED")
    model = _MODEL[layer]
    async with TestSessionLocal() as s:
        loaded = (await s.execute(select(model).where(model.id == row_id))).scalars().all()
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, model, row_id, intended="RELEASED") is True
    before = await _ledger_state(layer, row_id)

    reattempt = {"l1": _reattempt_rows, "l2": _reattempt_l2_rows, "l3": _reattempt_l3_rows}
    with (
        patch("app.services.nats_consumer._fetch_device", AsyncMock(return_value=None)),
        patch("app.services.nats_consumer._fetch_template", AsyncMock(return_value=None)),
        patch("app.services.driver_sandbox.execute_driver_method") as driver,
    ):
        outcomes = await reattempt[layer](loaded, _db_session_factory(), WiringRowClaims(), set())

    assert [o["outcome"] for o in outcomes] == ["in_progress"]
    assert await _ledger_state(layer, row_id) == before
    assert driver.call_count == 0


# --- Every retry-path write respects the claim, by the database (issue #1479) --
#
# The settles a retry pass makes on rows it never claimed (the stale-build parks, the
# supersession releases, the L2 nil-allocation release, the L3 unapplied-pin release)
# run only on a row nobody holds; on a row another pass holds they change nothing and
# the row reads back `in_progress`. Each test runs the real pass both ways: held by
# another pass (refused) and free (the settle happens), so it fails in either
# direction. The holder side: whether a pass still holds a row is the row's stamp in
# the database, so a holder whose claim expired and was taken over is refused like
# anyone else, while one whose expired claim nobody took still writes.

OTHER_RES = uuid.uuid4()


async def _held_by_another_pass(model, row_id) -> datetime:
    """Claim the row with a separate pass's ledger; return the stamp it wrote."""
    other = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert (
            await other.claim(s, model, row_id, intended=await _true_direction(model, row_id))
            is True
        )
    return other.held[row_id]


async def _loaded(model, row_id):
    async with TestSessionLocal() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalars().all()


async def _ledger_row(model, row_id):
    async with TestSessionLocal() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalar_one()


def _retry_pass_patches(**extra):
    """The reads a retry pass makes before its writes: intent is gone (an empty fork),
    the switch is gone (nothing reaches a driver), and the L3 derivations are empty."""
    patches = [
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            AsyncMock(return_value=[]),
        ),
        patch("app.services.nats_consumer._fetch_device", AsyncMock(return_value=None)),
        patch("app.services.nats_consumer._fetch_template", AsyncMock(return_value=None)),
        patch("app.services.nats_consumer._derive_l3_adjacency", AsyncMock(return_value={})),
        patch("app.services.nats_consumer._wired_ports_by_device", lambda wires: {}),
    ]
    patches += [patch(target, value) for target, value in extra.items()]
    return patches


async def _run_pass(layer, loaded, *patches):
    from app.services.wiring_retry_service import (
        _reattempt_l2_rows,
        _reattempt_l3_rows,
        _reattempt_rows,
    )

    reattempt = {"l1": _reattempt_rows, "l2": _reattempt_l2_rows, "l3": _reattempt_l3_rows}
    with ExitStack() as stack:
        driver = stack.enter_context(patch("app.services.driver_sandbox.execute_driver_method"))
        for p in patches:
            stack.enter_context(p)
        outcomes = await reattempt[layer](loaded, _db_session_factory(), WiringRowClaims(), set())
    assert driver.call_count == 0
    return [o["outcome"] for o in outcomes]


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
@pytest.mark.parametrize("layer", LAYERS)
async def test_a_stale_build_park_skips_a_row_another_pass_holds(layer, held):
    """Finding 1 of the #1479 review: a pass whose copy of a build row is stale (the
    fork no longer intends it) parks it FAILED intended RELEASED with attempts 0 and
    the claim cleared. On a row another pass is driving that handed the row to a third
    pass mid-drive; the park must be refused there."""
    from app.services.nats_consumer import WIRING_STALE_BUILD_REASON

    row_id = await _seed_layer(layer, intended="ACTIVE")
    loaded = await _loaded(_MODEL[layer], row_id)
    if held:
        await _held_by_another_pass(_MODEL[layer], row_id)
    before = await _ledger_state(layer, row_id)

    outcomes = await _run_pass(layer, loaded, *_retry_pass_patches())

    after = await _ledger_state(layer, row_id)
    if held:
        assert outcomes == ["in_progress"]
        assert after == before, "a park refused on a held row changes nothing"
    else:
        assert outcomes == ["still_failed"]
        assert after == ("FAILED", 0, WIRING_STALE_BUILD_REASON, None)


async def _seed_l1_active_other():
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=OTHER_RES,
                switch_device_id=SWITCH_ID,
                port_a="0/0/1",
                port_b="0/0/9",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()


async def _seed_l2_active_other():
    async with TestSessionLocal() as s:
        va = VlanAssignment(reservation_id=OTHER_RES, fabric_id=uuid.uuid4(), vlan_id=200)
        s.add(va)
        await s.flush()
        s.add(
            L2PortAssignment(
                reservation_id=OTHER_RES,
                vlan_assignment_id=va.id,
                switch_device_id=SWITCH_ID,
                port="0/0/1",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_supersession_settle_skips_a_row_another_pass_holds(layer, held):
    """A release whose port another reservation now holds ACTIVE is settled RELEASED
    with no driver call; on a row another pass is driving, it is not."""
    row_id = await _seed_layer(layer, intended="RELEASED")
    await (_seed_l1_active_other() if layer == "l1" else _seed_l2_active_other())
    loaded = await _loaded(_MODEL[layer], row_id)
    if held:
        await _held_by_another_pass(_MODEL[layer], row_id)
    before = await _ledger_state(layer, row_id)

    outcomes = await _run_pass(layer, loaded, *_retry_pass_patches())

    after = await _ledger_state(layer, row_id)
    if held:
        assert outcomes == ["in_progress"]
        assert after == before
    else:
        assert outcomes == ["superseded"]
        assert after[0] == "RELEASED"


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
async def test_the_l2_nil_allocation_release_skips_a_row_another_pass_holds(held):
    """A build row with no usable allocation whose intent is gone is released directly
    (no VLAN number to remove); on a row another pass is driving, it is not."""
    row_id = await _seed_l2_failed(uuid.UUID(int=0), intended="ACTIVE")
    loaded = await _loaded(L2PortAssignment, row_id)
    if held:
        await _held_by_another_pass(L2PortAssignment, row_id)
    before = await _ledger_state("l2", row_id)

    outcomes = await _run_pass("l2", loaded, *_retry_pass_patches())

    after = await _ledger_state("l2", row_id)
    if held:
        assert outcomes == ["in_progress"]
        assert after == before
    else:
        assert outcomes == ["released"]
        assert after[0] == "RELEASED"


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
async def test_the_l3_unapplied_pin_release_skips_a_row_another_pass_holds(held):
    """A build pin with no routes from any source is released (WIRE-L3-4); on a row
    another pass is driving, it is not, and the pass reports it in_progress rather than
    still_failed."""
    async with TestSessionLocal() as s:
        row = RouteAssignment(
            reservation_id=RES_ID,
            device_id=SWITCH_ID,
            routes=[],
            status="FAILED",
            intended="ACTIVE",
            attempts=0,
            last_error="driver timeout",
        )
        s.add(row)
        await s.commit()
        row_id = row.id
    loaded = await _loaded(RouteAssignment, row_id)
    if held:
        await _held_by_another_pass(RouteAssignment, row_id)
    before = await _ledger_state("l3", row_id)

    outcomes = await _run_pass(
        "l3",
        loaded,
        *_retry_pass_patches(
            **{
                "app.services.nats_consumer._derive_l3_adjacency": AsyncMock(
                    return_value={str(SWITCH_ID): set()}
                ),
                "app.services.nats_consumer._fetch_latest_config": AsyncMock(
                    return_value={"config": {"routes": []}}
                ),
            }
        ),
    )

    after = await _ledger_state("l3", row_id)
    if held:
        assert outcomes == ["in_progress"]
        assert after == before
    else:
        assert outcomes == ["released"]
        assert after[0] == "RELEASED"


async def _superseded_holder(layer, intended="ACTIVE"):
    """Pass A claims the row with a claim that is already expired; pass B takes it over.

    Returns (row_id, A's ledger). A still lists the row in `held`: only the database
    knows A no longer holds it.
    """
    row_id = await _seed_layer(layer, intended=intended)
    stale = WiringRowClaims(budget=timedelta(seconds=-60))
    async with TestSessionLocal() as s:
        assert await stale.claim(s, _MODEL[layer], row_id, intended=intended) is True
    await _held_by_another_pass(_MODEL[layer], row_id)
    assert row_id in stale.held
    return row_id, stale


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_superseded_holder_cannot_record_a_failure(db, layer):
    """Finding 2 of the #1479 review: `held` was in-process belief. A holder whose
    claim expired and was taken over must not free the new holder's claim."""
    row_id, stale = await _superseded_holder(layer)
    before = await _ledger_state(layer, row_id)

    assert await _write_failure(layer, db, row_id, stale) is None
    assert await _ledger_state(layer, row_id) == before
    assert stale.lost == {row_id}


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_holder_whose_expired_claim_nobody_took_still_writes(db, layer):
    """The stamp is the holder's until someone replaces it: an expired claim nobody
    took over still matches, so the holder's own writes land."""
    row_id = await _seed_layer(layer)
    late = WiringRowClaims(budget=timedelta(seconds=-60))
    assert await late.claim(db, _MODEL[layer], row_id, intended="ACTIVE") is True

    assert await _write_failure(layer, db, row_id, late) is not None
    assert await _ledger_state(layer, row_id) == ("FAILED", 2, "again", None)
    assert late.lost == set()


SUCCESS = {"success": True, "output": {}, "error": None, "duration_ms": 1}
FAILURE = {"success": False, "output": None, "error": "boom", "duration_ms": 1}
L3_SWITCH = {
    "id": str(SWITCH_ID),
    "name": "l3",
    "template_id": "t",
    "driver_id": str(uuid.uuid4()),
    "driver_sha256": "sha",
    "driver_filename": "d.zip",
    "connection_type": "Layer 3 Switch",
    "field_data": {},
}


async def _drive_one(layer, row_id, claims, result, *, build=True):
    """The holder's own per-row apply (L1, L2) with the sandbox answering `result`."""
    from app.services.nats_consumer import _apply_one_port_action, _apply_one_vlan_action

    va = (await _ledger_row(L2PortAssignment, row_id)).vlan_assignment_id if layer == "l2" else None
    with patch("app.services.driver_sandbox.execute_driver_method", return_value=result):
        async with TestSessionLocal() as s:
            if layer == "l1":
                await _apply_one_port_action(
                    s,
                    "connect_ports" if build else "disconnect_ports",
                    "/tmp/d",
                    {},
                    set(),
                    SWITCH_ID,
                    uuid.uuid4(),
                    "sha",
                    RES_ID,
                    "0/0/1",
                    "0/0/2",
                    None,
                    retry_row_id=row_id,
                    claims=claims,
                )
            else:
                await _apply_one_vlan_action(
                    s,
                    "add_to_vlan" if build else "remove_from_vlan",
                    "/tmp/d",
                    {},
                    set(),
                    SWITCH_ID,
                    uuid.uuid4(),
                    "sha",
                    RES_ID,
                    "0/0/1",
                    100,
                    va,
                    retry_row_id=row_id,
                    claims=claims,
                )


async def _drive_l3(row_id, claims, *, provision, routes, failing=()):
    """The holder's own L3 per-switch apply; `failing` names actions or route
    destinations whose driver call fails."""
    import httpx
    from app.services.nats_consumer import _apply_l3_adjacency, _FetchContext

    def _driver(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        if action in failing or mk.get("destination") in failing:
            return FAILURE
        return SUCCESS

    item = {"device_id": str(SWITCH_ID), "routes": routes}
    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.nats_consumer._fetch_device", AsyncMock(return_value=L3_SWITCH))
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template",
                AsyncMock(return_value={"id": "t", "sections": []}),
            )
        )
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", AsyncMock(return_value="/tmp/d"))
        )
        stack.enter_context(
            patch("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={}))
        )
        driver = stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=_driver)
        )
        async with httpx.AsyncClient() as client:
            await _apply_l3_adjacency(
                str(RES_ID),
                [] if provision else [item],
                [item] if provision else [],
                _FetchContext(client),
                _db_session_factory(),
                retry_row_ids={str(SWITCH_ID): row_id},
                claims=claims,
            )
    assert driver.call_count > 0
    return driver


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_superseded_holders_failure_is_refused_but_its_success_is_recorded(layer):
    """A holder that believes it holds the row (it is in `held`) but whose claim another
    pass took over: its FAILURE write needs its own stamp and is refused (the row and the
    new holder's claim stay, the row reads in_progress); its SUCCESS flip is the
    unconditional writer, exactly as on main (issue #1481), so it is recorded."""
    row_id, stale = await _superseded_holder(layer)
    before = await _ledger_state(layer, row_id)

    await _drive_one(layer, row_id, stale, FAILURE)
    assert await _ledger_state(layer, row_id) == before
    assert stale.lost == {row_id}

    await _drive_one(layer, row_id, stale, SUCCESS)
    assert (await _ledger_state(layer, row_id))[0] == "ACTIVE", "the success flip is main's"


@pytest.mark.parametrize("failing", ["login", "remove_route", None])
async def test_a_superseded_l3_holder_records_only_its_success(failing):
    """The L3 per-switch apply, the same rule: a superseded holder's login failure and
    route failure are refused (another pass holds a live claim, so even the partial-
    failure write, which lands over a merely cleared claim, is refused); its successful
    release is main's unconditional writer."""
    row_id, stale = await _superseded_holder("l3", intended="RELEASED")
    before = await _ledger_state("l3", row_id)

    await _drive_l3(
        row_id,
        stale,
        provision=False,
        routes=[{"destination": "10.0.0.0/24"}],
        failing=(failing,) if failing else (),
    )
    if failing:
        assert await _ledger_state("l3", row_id) == before
        assert stale.lost == {row_id}
    else:
        assert (await _ledger_state("l3", row_id))[0] == "RELEASED"


async def _cleared_by_the_reconcile(layer, row_id, how):
    """A writer that never claims clears the holder's claim: the reconcile's key-based
    failure write (`failure`) or its stale-build park (`park`)."""
    from app.services.nats_consumer import WIRING_STALE_BUILD_REASON

    async with TestSessionLocal() as s:
        if how == "park":
            park = {
                "l1": park_stale_l1_build,
                "l2": park_stale_l2_build,
                "l3": park_stale_route_build,
            }[layer]
            await park(s, row_id, WIRING_STALE_BUILD_REASON)
        elif layer == "l1":
            await record_l1_failed(
                s, RES_ID, SWITCH_ID, "0/0/1", "0/0/2", 1, "reconcile", intended="ACTIVE"
            )
        elif layer == "l2":
            await record_l2_failed(
                s, RES_ID, None, SWITCH_ID, "0/0/1", 1, "reconcile", intended="ACTIVE"
            )
        else:
            await record_route_failed(s, RES_ID, SWITCH_ID, None, 1, "reconcile", intended="ACTIVE")
    assert await _claimed_until(_MODEL[layer], row_id) is None


@pytest.mark.parametrize("layer", LAYERS)
async def test_after_the_reconcile_clears_the_claim_the_holders_success_is_recorded(layer):
    """Main's behaviour, pinned (issue #1481 owns any change): the holder's successful
    connect, join, or provision is recorded ACTIVE by the unconditional writer even
    though the reconcile's failure write cleared its claim during the drive."""
    row_id = await _seed_layer(layer, intended="ACTIVE")
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, _MODEL[layer], row_id, intended="ACTIVE") is True
    await _cleared_by_the_reconcile(layer, row_id, "failure")

    if layer == "l3":
        await _drive_l3(row_id, holder, provision=True, routes=[{"destination": "10.0.0.0/24"}])
    else:
        await _drive_one(layer, row_id, holder, SUCCESS)

    assert (await _ledger_state(layer, row_id))[0] == "ACTIVE"
    assert holder.lost == set()


@pytest.mark.parametrize("how", ["failure", "park"])
@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_after_the_reconcile_clears_the_claim_the_holders_failure_is_refused(layer, how):
    """The one difference from main on the holder's side: an L1 or L2 driver call that
    failed changed nothing on the device, so its failure write needs the holder's own
    stamp. After the reconcile's failure write or stale park cleared it, the reconcile's
    record stands (main overwrote it: a park became intended ACTIVE again)."""
    row_id = await _seed_layer(layer, intended="ACTIVE")
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, _MODEL[layer], row_id, intended="ACTIVE") is True
    await _cleared_by_the_reconcile(layer, row_id, how)
    after_reconcile = await _ledger_state(layer, row_id)

    await _drive_one(layer, row_id, holder, FAILURE)

    assert await _ledger_state(layer, row_id) == after_reconcile


@pytest.mark.parametrize("how", ["failure", "park"])
async def test_after_the_reconcile_clears_the_claim_an_l3_partial_failure_is_recorded(how):
    """An L3 switch whose route calls partly failed may have configured routes only its
    failure write records (#1001), so that write lands as on main over a claim the
    reconcile cleared: FAILED intended ACTIVE, with every route it may have installed."""
    row_id = await _seed_layer("l3", intended="ACTIVE")
    holder = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await holder.claim(s, RouteAssignment, row_id, intended="ACTIVE") is True
    await _cleared_by_the_reconcile("l3", row_id, how)

    await _drive_l3(
        row_id,
        holder,
        provision=True,
        routes=[{"destination": "10.0.1.0/24"}, {"destination": "10.0.2.0/24"}],
        failing=("10.0.2.0/24",),
    )

    row = await _ledger_row(RouteAssignment, row_id)
    assert (row.status, row.intended, row.last_error) == ("FAILED", "ACTIVE", "boom")
    assert {r["destination"] for r in row.routes} >= {"10.0.1.0/24", "10.0.2.0/24"}
    assert holder.lost == set()


async def test_write_guard_does_nothing_without_a_ledger(db):
    """The reconcile passes no claim ledger: the guard yields True on a row another
    pass holds without reading the row, so every reconcile write is exactly as before."""
    row_id = await _seed_l1_failed()
    await _held_by_another_pass(L1ConnectionAssignment, row_id)
    with patch.object(db, "execute", AsyncMock(side_effect=AssertionError("no read"))):
        async with write_guard(db, L1ConnectionAssignment, row_id, None) as ok:
            assert ok is True
        async with write_guard(db, L1ConnectionAssignment, None, WiringRowClaims()) as ok:
            assert ok is True


async def test_write_guard_ends_its_transaction_and_rolls_back_on_an_error():
    """The guard's lock lives in the caller's session, which for a holder is the
    per-switch session that goes on to the switch's next driver call. Exiting commits
    even when the body wrote nothing, so the row is not left locked; an error inside
    rolls the body's writes back and propagates."""
    row_id = await _seed_l1_failed()
    async with TestSessionLocal() as s:
        async with write_guard(s, L1ConnectionAssignment, row_id, WiringRowClaims()) as ok:
            assert ok is True
            assert s.in_transaction()
        assert not s.in_transaction()

        with pytest.raises(RuntimeError):
            async with write_guard(s, L1ConnectionAssignment, row_id, WiringRowClaims()) as ok:
                row = await s.get(L1ConnectionAssignment, row_id)
                row.last_error = "never committed"
                await s.flush()
                raise RuntimeError("boom")
        assert not s.in_transaction()
    assert (await _ledger_state("l1", row_id))[2] == "driver timeout"


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


async def _race_held_loser(winner_call, loser_call):
    """Run two real ledger writes against one FAILED row, the loser held at its CAS.

    The loser runs on its own session and is held at its first UPDATE (the CAS),
    after its own FAILED-row SELECT, so the row it flips is attached to that
    session (as in production, unlike the patched siblings above, whose stale row
    comes from a closed session the loser's rollback cannot expire). The winner
    runs to completion on another session, then the loser is released. Returns
    (winner's row, loser's row).
    """
    import asyncio

    from sqlalchemy.sql.dml import Update

    async with TestSessionLocal() as db_win, TestSessionLocal() as db_lose:
        at_update = asyncio.Event()
        release = asyncio.Event()
        real_execute = db_lose.execute

        async def _held_execute(statement, *args, **kwargs):
            if isinstance(statement, Update) and not at_update.is_set():
                at_update.set()
                await release.wait()
            return await real_execute(statement, *args, **kwargs)

        db_lose.execute = _held_execute
        loser = asyncio.create_task(loser_call(db_lose))
        await asyncio.wait_for(at_update.wait(), timeout=5)
        won = await winner_call(db_win)
        release.set()
        lost = await asyncio.wait_for(loser, timeout=5)
    return won, lost


def _stale_writer_skips(caplog) -> int:
    return sum(r.getMessage().startswith("stale reattempt writer skipped") for r in caplog.records)


async def test_l1_success_flip_cas_loser_with_attached_row_returns_the_winner(caplog):
    """Issue #1152: two real record_l1_connect calls race on one FAILED pair. The
    loser's CAS matches zero rows; it returns the winner's row (carrying the
    physical connection id only the winner set) and logs the stale-writer skip,
    instead of raising MissingGreenlet on the expired row after its rollback."""
    import logging

    row_id = await _seed_l1_failed()
    phys = uuid.uuid4()
    caplog.set_level(logging.INFO, logger="app.services.l1_assignment_service")

    won, lost = await _race_held_loser(
        lambda s: record_l1_connect(
            s, RES_ID, SWITCH_ID, "0/0/1", "0/0/2", physical_connection_id=str(phys)
        ),
        lambda s: record_l1_connect(s, RES_ID, SWITCH_ID, "0/0/1", "0/0/2"),
    )

    assert won.id == row_id
    assert won.status == "ACTIVE"
    assert lost is not None
    assert lost.id == row_id
    assert lost.status == "ACTIVE"
    assert lost.physical_connection_id == phys, "the loser returns the winner's row"
    assert _stale_writer_skips(caplog) == 1
    async with TestSessionLocal() as s:
        fresh = await s.get(L1ConnectionAssignment, row_id)
        assert (fresh.status, fresh.physical_connection_id) == ("ACTIVE", phys)


async def test_l2_success_flip_cas_loser_with_attached_row_returns_the_winner(caplog):
    """Issue #1152, the record_l2_membership_active mirror: the loser returns the
    winner's row (pointing at the winner's VLAN allocation) and logs the
    stale-writer skip; the loser's allocation is never written."""
    import logging

    seeded_va = await _seed_alloc()
    winner_va = await _seed_alloc()
    loser_va = await _seed_alloc()
    row_id = await _seed_l2_failed(seeded_va)
    caplog.set_level(logging.INFO, logger="app.services.l2_membership_service")

    won, lost = await _race_held_loser(
        lambda s: record_l2_membership_active(s, RES_ID, winner_va, SWITCH_ID, "0/0/1"),
        lambda s: record_l2_membership_active(s, RES_ID, loser_va, SWITCH_ID, "0/0/1"),
    )

    assert won.id == row_id
    assert won.status == "ACTIVE"
    assert lost is not None
    assert lost.id == row_id
    assert lost.status == "ACTIVE"
    assert lost.vlan_assignment_id == winner_va, "the loser returns the winner's row"
    assert _stale_writer_skips(caplog) == 1
    async with TestSessionLocal() as s:
        fresh = await s.get(L2PortAssignment, row_id)
        assert (fresh.status, fresh.vlan_assignment_id) == ("ACTIVE", winner_va)


# --- An L3 pin's one claim outlives its whole drive (issue #1482) -------------
#
# The tests below run on a virtual clock: `wiring_claim`'s `datetime.now` reads it,
# every sandbox attempt advances it by the full driver timeout, and every in-line
# backoff sleep advances it by its delay. So a drive takes its exact worst case,
# (R + 2) x 90.6s at the defaults, in no wall-clock time, and the tests hold pinned
# to one core with no timing bound at all. The switch and template reads and the
# driver load before the claim take virtual time too, so a claim taken before them
# shows in its stamp, and a test can add a fixed overhead to every driver action
# (time outside the sandbox timers, which the per-action allowance covers).


class _VirtualClock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.started = self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def datetime_class(self):
        clock = self

        class _ClockDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now

        return _ClockDatetime


def _virtual_clock_patches(clock: _VirtualClock, stack: ExitStack) -> None:
    """Route `wiring_claim`'s clock and `nats_consumer`'s backoff sleeps through
    `clock`. Only `nats_consumer` sees the virtual sleep: its `asyncio` name is
    swapped for a proxy, so the event loop's own sleeps are untouched."""
    import asyncio

    class _AsyncioWithVirtualSleep:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay, *args, **kwargs):
            clock.advance(delay)
            await asyncio.sleep(0)

    stack.enter_context(patch("app.services.wiring_claim.datetime", clock.datetime_class()))
    stack.enter_context(patch("app.services.nats_consumer.asyncio", _AsyncioWithVirtualSleep()))


# Virtual seconds the reads before the claim take: the switch and the template reads,
# then the driver load (a package download has a 30s timeout).
DEVICE_READ_S = 10
TEMPLATE_READ_S = 10
DRIVER_LOAD_S = 30


async def _drive_l3_worst_case(
    row_id,
    holder,
    *,
    provision,
    routes,
    through_retry=False,
    overhead_ms=0,
    record_runs=True,
    install_s=0,
):
    """Drive one L3 pin with `holder`'s claim, every driver action taking its worst
    case: three attempts, each running to the driver timeout, with both backoff
    sleeps between them, plus `overhead_ms` of virtual time when its execution run is
    created (time outside the sandbox timers). The login succeeds on its third
    attempt so the route operations run; every route operation and the logout fail
    all three. When the logout's run is recorded (the drive's last driver action has
    finished), a second retry pass tries to claim the pin at that instant.

    `through_retry` drives a removal the way the retry channels do, through
    `_reattempt_l3_rows` on the loaded FAILED row (which drives the row's own
    `routes`); otherwise `_apply_l3_adjacency` is called directly with `routes`.
    `record_runs` False stands in a bare object for each execution run instead of
    writing it, so the sweep over every route count stays fast; the claim and ledger
    writes are real either way. `install_s` is the virtual time each dependency
    install takes (`driver_sandbox.prepare_driver_dependencies`, issue #1495); every
    install is counted in `observed["installs"]`.

    Returns what was observed: the virtual time the driver load finished, the first
    driver call, the logout finished, whether the rival's claim succeeded, and the
    driver calls made."""
    import httpx
    from app.services import execution_service
    from app.services.nats_consumer import (
        WIRING_DRIVER_ATTEMPTS,
        _apply_l3_adjacency,
        _FetchContext,
    )

    clock = _VirtualClock()
    observed: dict = {"calls": [], "first_call_at": None, "installs": 0}
    attempts: dict = {}

    def _install(driver_path):
        observed["installs"] += 1
        clock.advance(install_s)
        return "failed"

    def _driver(driver_path, action, context, **kwargs):
        key = (action, (kwargs.get("method_kwargs") or {}).get("destination"))
        attempts[key] = attempts.get(key, 0) + 1
        if observed["first_call_at"] is None:
            observed["first_call_at"] = clock.now
        clock.advance(settings.execution_timeout_seconds)
        observed["calls"].append(action)
        if action == "login" and attempts[key] == WIRING_DRIVER_ATTEMPTS:
            return SUCCESS
        return FAILURE

    def _after(seconds, value, key=None):
        clock.advance(seconds)
        if key is not None:
            observed[key] = clock.now
        return value

    real_create = execution_service.create_execution_run

    async def _create(*args, **kwargs):
        clock.advance(overhead_ms / 1000)
        if not record_runs:
            return SimpleNamespace(action=args[4])
        return await real_create(*args, **kwargs)

    real_update = execution_service.update_execution_run

    async def _update(db, run, status, **kwargs):
        result = await real_update(db, run, status, **kwargs) if record_runs else run
        if run.action == "logout":
            observed["logout_finished_at"] = clock.now
            rival = WiringRowClaims()
            async with TestSessionLocal() as s:
                observed["rival_claimed"] = await rival.claim(
                    s,
                    RouteAssignment,
                    row_id,
                    intended=await _true_direction(RouteAssignment, row_id),
                )
        return result

    item = {"device_id": str(SWITCH_ID), "routes": routes}
    with ExitStack() as stack:
        _virtual_clock_patches(clock, stack)
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                AsyncMock(side_effect=lambda *a, **k: _after(DEVICE_READ_S, L3_SWITCH)),
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template",
                AsyncMock(
                    side_effect=lambda *a, **k: _after(TEMPLATE_READ_S, {"id": "t", "sections": []})
                ),
            )
        )
        stack.enter_context(
            patch(
                "app.services.driver_loader.load_driver",
                AsyncMock(side_effect=lambda *a, **k: _after(DRIVER_LOAD_S, "/tmp/d", "loaded_at")),
            )
        )
        stack.enter_context(
            patch("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={}))
        )
        # The sandbox seam itself, not the worker thread behind it: these drives make up
        # to 906 driver calls each, and the thread hop is time outside the sandbox
        # timers, which `overhead_ms` stands for on the virtual clock.
        stack.enter_context(
            patch("app.services.nats_consumer._run_sandbox", AsyncMock(side_effect=_driver))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.prepare_driver_dependencies", side_effect=_install)
        )
        stack.enter_context(
            patch("app.services.execution_service.create_execution_run", side_effect=_create)
        )
        stack.enter_context(
            patch("app.services.execution_service.update_execution_run", side_effect=_update)
        )
        if through_retry:
            from app.services.wiring_retry_service import _reattempt_l3_rows

            assert not provision, "the retry drives a build only after an intent fetch"
            observed["outcomes"] = await _reattempt_l3_rows(
                await _loaded(RouteAssignment, row_id), _db_session_factory(), holder, set()
            )
        else:
            async with httpx.AsyncClient() as client:
                await _apply_l3_adjacency(
                    str(RES_ID),
                    [] if provision else [item],
                    [item] if provision else [],
                    _FetchContext(client),
                    _db_session_factory(),
                    retry_row_ids={str(SWITCH_ID): row_id},
                    claims=holder,
                )
    observed["clock_started"] = clock.started
    return observed


def _routes(count: int) -> list[dict]:
    return [
        {"destination": f"10.{i // 250 + 1}.{i % 250}.0/24", "next_hop": "10.0.0.1"}
        for i in range(count)
    ]


@pytest.mark.parametrize(
    ("provision", "through_retry", "route_count", "route_operations"),
    [
        # The retry channels' own removal pass: login and logout only for a pin that
        # records no routes, then one remove_route per pinned route.
        (False, True, 0, 0),
        (False, True, 1, 1),
        (False, True, 2, 2),
        (False, True, 10, 10),
        (False, True, 200, 200),
        # A provision over the seeded FAILED pin also removes that pin's one route its
        # own set drops (the #1001 residue): three configures plus one removal.
        (True, False, 3, 4),
    ],
)
async def test_an_l3_pins_claim_is_still_held_when_its_last_driver_action_finishes(
    provision, through_retry, route_count, route_operations
):
    """The invariant, driven: with every action at its worst case, the stamp the L3
    apply writes is the claim instant plus the budget of its exact drive sequence,
    the drive's last action (the logout) finishes before that stamp, and a second
    pass that tries to claim the pin at that instant loses (issue #1482). Afterwards
    the holder's own record clears the claim, the normal release path."""
    routes = _routes(route_count)
    row_id = await _seed_l3_failed(
        intended="ACTIVE" if provision else "RELEASED",
        routes=None if provision else routes,
    )
    holder = WiringRowClaims()

    observed = await _drive_l3_worst_case(
        row_id, holder, provision=provision, routes=routes, through_retry=through_retry
    )

    actions = route_operations + 2
    assert len(observed["calls"]) == 3 * actions, "every action made all three attempts"
    drive = observed["logout_finished_at"] - observed["loaded_at"]
    assert drive == timedelta(milliseconds=actions * _worst_case_action_ms(30))
    assert observed["rival_claimed"] is False, "a second pass claimed the pin mid-drive"
    # The claim is taken after the driver load and immediately before the login: the
    # reads and the load took virtual time, and none passed between the load and the
    # first driver call.
    assert observed["loaded_at"] == observed["clock_started"] + timedelta(
        seconds=DEVICE_READ_S + TEMPLATE_READ_S + DRIVER_LOAD_S
    )
    assert observed["first_call_at"] == observed["loaded_at"]
    stamp = holder.held[row_id]
    assert stamp > observed["logout_finished_at"]
    assert stamp == observed["loaded_at"] + l3_pin_claim_budget(route_operations)
    assert await _claimed_until(RouteAssignment, row_id) is None
    if through_retry:
        # A removal with no route calls succeeds; one whose route calls all failed
        # stays FAILED. Never in_progress: the holder kept its claim to the end.
        expected = "released" if route_operations == 0 else "still_failed"
        assert [o["outcome"] for o in observed["outcomes"]] == [expected]


@pytest.mark.parametrize("route_count", [0, 1, 2, 10])
async def test_with_pip_install_on_an_l3_pins_claim_outlives_one_install_per_action(route_count):
    """Issue #1495, driven: with ALLOW_DRIVER_PIP_INSTALL on and every install running
    to its 60s timeout and failing (so it would be due again), each action installs
    once, not once per attempt, and the pin is still held when its logout finishes;
    a second pass that tries to claim it then loses. Before the fix each attempt
    installed again, 270.6s per action, and the 5-minute claim of a pin with no
    routes ran out after its first action."""
    row_id = await _seed_l3_failed(intended="RELEASED", routes=_routes(route_count))
    holder = WiringRowClaims()
    with patch.object(settings, "allow_driver_pip_install", True):
        observed = await _drive_l3_worst_case(
            row_id,
            holder,
            provision=False,
            routes=_routes(route_count),
            through_retry=True,
            install_s=INSTALL_MS / 1000,
        )
        budget = l3_pin_claim_budget(route_count)

    actions = route_count + 2
    assert observed["installs"] == actions, "one install per action, not per attempt"
    assert len(observed["calls"]) == 3 * actions
    drive = observed["logout_finished_at"] - observed["loaded_at"]
    assert drive == timedelta(milliseconds=actions * (INSTALL_MS + _worst_case_action_ms(30)))
    assert observed["rival_claimed"] is False, "a second pass claimed the pin mid-drive"
    assert holder.held[row_id] == observed["loaded_at"] + budget
    assert holder.held[row_id] > observed["logout_finished_at"]


async def test_a_raised_driver_timeout_still_keeps_an_l3_pins_claim_through_its_drive():
    """The budget scales with EXECUTION_TIMEOUT_SECONDS and so does the drive: at 120s
    a pin of five routes is still held when its logout finishes."""
    row_id = await _seed_l3_failed(intended="RELEASED")
    holder = WiringRowClaims()
    with patch.object(settings, "execution_timeout_seconds", 120):
        observed = await _drive_l3_worst_case(
            row_id, holder, provision=False, routes=_routes(5), through_retry=False
        )
        expected_budget = l3_pin_claim_budget(5)
    assert expected_budget == timedelta(minutes=43)  # 7 x 361.6s = 2531.2s, rounded up
    drive = observed["logout_finished_at"] - observed["loaded_at"]
    assert drive == timedelta(milliseconds=7 * _worst_case_action_ms(120))
    assert observed["rival_claimed"] is False, "a second pass claimed the pin mid-drive"
    assert holder.held[row_id] > observed["logout_finished_at"]
    assert holder.held[row_id] == observed["loaded_at"] + expected_budget


@pytest.mark.parametrize(
    ("layer", "build", "minutes"),
    [("l1", True, 5), ("l1", False, 5), ("l2", False, 5), ("l2", True, 7)],
)
async def test_l1_and_l2_rows_claim_with_their_row_budget(layer, build, minutes):
    """The L1 apply and an L2 remove pass no per-call budget: the stamp is the claim
    instant plus the unchanged 5-minute one-row budget. An L2 add is sized for one more
    driver action, the undo of a join that lost its allocation (issue #1413):
    `claim_budget(4)`, 7 minutes at the defaults. On the virtual clock exactly."""
    from app.services.wiring_claim import claim_budget

    row_id = await _seed_layer(layer, intended="ACTIVE" if build else "RELEASED")
    holder = WiringRowClaims()
    clock = _VirtualClock()
    with ExitStack() as stack:
        _virtual_clock_patches(clock, stack)
        await _drive_one(layer, row_id, holder, FAILURE, build=build)
    assert holder.held[row_id] == clock.started + timedelta(minutes=minutes)
    if layer == "l2" and build:
        assert claim_budget(4) == timedelta(minutes=7)


async def test_a_per_call_budget_sets_only_that_claims_stamp(db):
    """`claim(..., budget=)` stamps now plus that budget; the ledger's own budget is
    untouched for the next claim, and a row the pass already holds keeps its stamp."""
    sized = await _seed_l3_failed(device_id=uuid.uuid4())
    plain = await _seed_l3_failed(device_id=uuid.uuid4())
    holder = WiringRowClaims()
    clock = _VirtualClock()
    with ExitStack() as stack:
        _virtual_clock_patches(clock, stack)
        assert await holder.claim(
            db, RouteAssignment, sized, intended="ACTIVE", budget=timedelta(minutes=42)
        )
        assert await holder.claim(db, RouteAssignment, plain, intended="ACTIVE")
        assert await holder.claim(
            db, RouteAssignment, sized, intended="ACTIVE", budget=timedelta(minutes=1)
        )
    assert holder.held[sized] == clock.started + timedelta(minutes=42)
    assert holder.held[plain] == clock.started + timedelta(minutes=5)
    assert holder.budget == timedelta(minutes=5)


async def test_a_sized_l3_claim_still_expires_at_its_stamp(db):
    """Conservation: a longer claim is still a claim that ends. A holder that dies
    after claiming a pin of 50 routes leaves it unclaimable until exactly its stamp
    and no longer; at the stamp a new pass takes it, with no reaper."""
    row_id = await _seed_l3_failed()
    dead = WiringRowClaims()
    clock = _VirtualClock()
    budget = l3_pin_claim_budget(50)
    assert budget == timedelta(minutes=80)  # 52 x 91.6s = 4763.2s, rounded up
    with ExitStack() as stack:
        _virtual_clock_patches(clock, stack)
        assert await dead.claim(db, RouteAssignment, row_id, intended="ACTIVE", budget=budget)
        clock.advance(budget.total_seconds() - 1)
        async with TestSessionLocal() as s:
            assert (
                await WiringRowClaims().claim(s, RouteAssignment, row_id, intended="ACTIVE")
                is False
            )
        clock.advance(1)
        assert clock.now == dead.held[row_id]
        async with TestSessionLocal() as s:
            assert (
                await WiringRowClaims().claim(s, RouteAssignment, row_id, intended="ACTIVE") is True
            )


@pytest.mark.parametrize("route_operations", range(301))
async def test_an_l3_pins_claim_outlives_its_drive_with_overhead_just_under_the_allowance(
    route_operations,
):
    """The guarantee the per-action allowance buys, driven for every R from 0 to 300:
    each driver action spends 999 ms outside its sandbox timers on top of its worst
    case, and the second pass that tries to claim the pin when the logout ends still
    loses (issue #1482). Without the allowance R = 47 already loses its claim at 13 ms
    per action (0.6s of rounding slack over 49 actions)."""
    row_id = await _seed_l3_failed(intended="RELEASED", routes=_routes(route_operations))
    holder = WiringRowClaims()

    observed = await _drive_l3_worst_case(
        row_id,
        holder,
        provision=False,
        routes=_routes(route_operations),
        through_retry=True,
        overhead_ms=ALLOWANCE_MS - 1,
        record_runs=False,
    )

    actions = route_operations + 2
    drive = observed["logout_finished_at"] - observed["loaded_at"]
    assert drive == timedelta(milliseconds=actions * (_worst_case_action_ms(30) + 999))
    assert observed["rival_claimed"] is False, "a second pass claimed the pin mid-drive"
    assert holder.held[row_id] > observed["logout_finished_at"]


async def test_known_limit_overhead_far_over_the_allowance_lets_a_rival_claim():
    """KNOWN LIMIT, pinned so it stays visible: the allowance is an allowance, not a
    bound. When each of the 49 actions of a 47-route pin spends 5s outside its sandbox
    timers, 4s over the allowance, the drive overruns the stamp by far more than the
    rounding slack (11.6s) and a second pass claims the pin before the logout ends.
    This is what shows the allowance, and nothing else, is what holds the claim."""
    row_id = await _seed_l3_failed(intended="RELEASED", routes=_routes(47))
    holder = WiringRowClaims()

    observed = await _drive_l3_worst_case(
        row_id,
        holder,
        provision=False,
        routes=_routes(47),
        through_retry=True,
        overhead_ms=5000,
    )

    assert holder.held[row_id] < observed["logout_finished_at"]
    assert observed["rival_claimed"] is True
