"""The per-row drive claim shared by the two wiring retry channels (issue #817).

Both retry channels (the manual endpoint's `reattempt_reservation` and the
background `run_wiring_retry_tick`) load hardware-retryable FAILED ledger rows and
drive them through the same consumer applies. Issue #814 made every retry FAILURE
write a row-identity compare-and-swap, so a late write can no longer corrupt the
ledger, but nothing stopped the two channels from calling the DRIVER twice for one
row, from two processes even (the tick starts in every execution replica). This
module is that missing claim.

Shape, and why it is this shape:

  - The claim is a `claimed_until` timestamp on the row, not a lock held across the
    driver call. A driver call takes seconds to minutes, and a database row lock
    held that long would pin a connection and a transaction per row in flight. A
    stamp costs one short transaction, and a process that dies mid-drive leaves a
    stamp that expires by itself: no reaper, no heartbeat, no liveness protocol.
  - The claim is taken PER ROW, immediately before that row's own driver call, not
    once at selection. A selected batch is driven sequentially (up to
    `wiring_retry_batch_size` rows, each behind a per-switch login), so the last row
    of a batch can reach its driver call minutes after the batch was selected; one
    stamp taken at selection time would have expired by then, which is exactly the
    window the claim exists to close.
  - `claim_row` is a compare-and-swap: `UPDATE ... WHERE id AND status = 'FAILED'
    AND (claimed_until IS NULL OR claimed_until <= now)`. A rowcount of zero means
    another channel holds the row (or a concurrent writer already moved it out of
    FAILED), so the caller drives nothing for it. The manual channel reports such a
    row as `in_progress`, the seventh retry outcome; the tick logs and skips it.

Who may use these readers, and who may not. `claimable_row_ids` and `unclaimed`
are for the RETRY CHANNELS ONLY. The wiring_changed reconcile in `nats_consumer`
reads FAILED rows too, through `failed_assignments_for_reservation`,
`failed_memberships_for_reservation`, and `failed_route_assignments_for_reservation`,
for its stale-build widening: FAILED intended-ACTIVE rows whose intent has gone join
the RELEASE side of the diff so a dead build is settled instead of being rebuilt
forever. Those three readers deliberately do NOT take the claim predicate. The
widening is a documented asymmetry that only holds while it sees every FAILED row:
the releases diff runs against ACTIVE plus FAILED intended-ACTIVE rows while the
builds diff stays ACTIVE-only, and hiding a claimed row from the releases side would
silently drop a row the reconcile is responsible for settling, in a pass that is
event-driven and may not come again. The retry channels are the only readers that
want "rows nobody else is driving", so they are the only ones that filter.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)


# Driver actions one L1 or L2 row's drive makes: the per-switch login, the row's own
# op, and the per-switch logout. The default claim budget is sized for this many.
ROW_DRIVER_ACTIONS = 3

# Issue #1482: an ALLOWANCE, not a measured bound, added per driver action to an L3
# pin's claim budget only, for the time each action spends outside its sandbox
# timeouts and backoff sleeps: the two execution-run writes around it, the worker
# thread hop, the sandbox's temp-file I/O, the child's fork and exec before the
# subprocess timer starts, and the kill and reap after a timeout. That time grows with
# the number of actions while the rounding slack does not, so a pin of many routes
# needs it; one second per action is far above what those steps take on a healthy
# host. L1 and L2 budgets do not include it.
L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS = 1000


def _worst_case_ms(driver_actions: int, extra_ms_per_action: int = 0) -> int:
    """The summed worst case of `driver_actions` actions, in whole milliseconds:
    every attempt running to the sandbox timeout, every backoff sleep, plus
    `extra_ms_per_action` per action."""
    from app.services.nats_consumer import (
        WIRING_DRIVER_ATTEMPTS,
        WIRING_DRIVER_BACKOFF_FACTOR,
        WIRING_DRIVER_INITIAL_DELAY,
        WIRING_DRIVER_MAX_DELAY,
    )

    backoff_ms_per_action = 0
    delay = WIRING_DRIVER_INITIAL_DELAY
    for _ in range(max(0, WIRING_DRIVER_ATTEMPTS - 1)):
        backoff_ms_per_action += round(min(delay, WIRING_DRIVER_MAX_DELAY) * 1000)
        delay *= WIRING_DRIVER_BACKOFF_FACTOR

    per_action_ms = (
        WIRING_DRIVER_ATTEMPTS * max(1, settings.execution_timeout_seconds) * 1000
        + backoff_ms_per_action
        + extra_ms_per_action
    )
    return driver_actions * per_action_ms


def _next_whole_minute_after(total_ms: int) -> timedelta:
    return timedelta(minutes=total_ms // 60_000 + 1)


def claim_budget(driver_actions: int = ROW_DRIVER_ACTIONS) -> timedelta:
    """How long a drive of `driver_actions` driver actions may take, in whole minutes.

    Derived from the values that already bound one driver action's worst case; no
    new setting, deliberately (issue #817):

      - `nats_consumer.WIRING_DRIVER_ATTEMPTS` (3) sandbox attempts per driver
        action, each a driver child that `driver_sandbox.execute_driver_method`
        stops at `settings.execution_timeout_seconds` (30s).
      - The in-line backoff between attempts:
        `WIRING_DRIVER_INITIAL_DELAY` (0.2s) then doubled by
        `WIRING_DRIVER_BACKOFF_FACTOR`, capped at `WIRING_DRIVER_MAX_DELAY`, for
        `attempts - 1` sleeps per action.

    So the bound is attempts * actions * timeout + actions * backoff, rounded UP to
    the next whole minute strictly after it. Rounding up is what keeps the expiry
    safely clear of a drive that is still running, and the whole-minute figure
    matches the health scheduler's flat five-minute CLAIM_WINDOW, the same idiom one
    layer up. Strictly after (issue #1482): a sum that is already a whole number of
    minutes gets the next minute, because a claim whose stamp equals the drive's end is
    reclaimable at that instant (`unclaimed` is `claimed_until <= now`). The sum is
    exact, in whole milliseconds, so no float error decides which minute that is. For
    one row (three actions) at any whole-second timeout the sum is never a whole number
    of minutes (it ends in .8 seconds), so this budget is the plain round-up it always
    was.

    Time outside the sum, which this budget does not count: each action's time
    outside its sandbox timers (the execution-run writes, the worker thread hop, the
    sandbox's temp-file I/O, the child's fork and exec, the kill and reap after a
    timeout), covered only by the rounding's slack; and, with
    `ALLOW_DRIVER_PIP_INSTALL` on, the `pip install` `execute_driver_method` runs
    before the driver child, with its own 60s timeout, on every attempt while a
    package that has a `requirements.txt` has no `_deps/` directory yet (an install
    that fails or times out before pip writes that directory leaves none, so it runs
    again on the next attempt), which makes that attempt's worst case 60s plus the
    timeout.

    The default, `ROW_DRIVER_ACTIONS` (3), is one L1 or L2 row: the per-switch
    `login`, the row's own connect/disconnect or add/remove op, and the per-switch
    `logout`. The login and logout are shared by every row on the switch, so charging
    all three to each row is deliberately generous. That is 3 * 3 * 30s = 270s plus
    1.8s of backoff, which rounds to 5 minutes at the default timeout. An L3 pin
    drives more than one op under its one claim, so it is sized by
    `l3_pin_claim_budget` instead (issue #1482).

    A deployment that raises `execution_timeout_seconds` gets a proportionally
    longer budget for free, which is the point of deriving it rather than pinning it.
    """
    return _next_whole_minute_after(_worst_case_ms(driver_actions))


def l3_pin_claim_budget(route_operations: int) -> timedelta:
    """The claim budget of one L3 pin whose drive makes `route_operations` route calls.

    Issue #1482. An L3 row is a whole per-switch route pin, claimed once before the
    switch's login, and that one claim must outlive the whole drive: the login, every
    route operation (the residue removals and one call per route of the pin, or a
    reconcile's removals and additions), and the logout, `route_operations + 2`
    driver actions in all. The caller counts the exact sequence it is about to drive,
    before it claims.

    The budget is N = `route_operations + 2` actions' worst case (`claim_budget`'s
    sum) plus `L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS` per action, rounded up to the
    next whole minute strictly after, and never shorter than the one-row budget
    (`claim_budget()`), so a pin of zero or one route operation keeps the budget it
    always had (5 minutes at the defaults). The guarantee: the claim outlives the drive
    as long as the time the drive spends outside its sandbox timers and backoff sleeps
    stays under N times the allowance plus the rounding slack (at least 0.4s at the
    default timeout, first at 129 route operations; at least 0.2s at any whole-second
    timeout), that is, under one second per action on average plus that slack. The
    `pip install` named in `claim_budget` is outside it.

    No cap, deliberately: a cap below the worst-case drive would let a second pass
    claim the pin while this one is still inside the driver, which is the double
    drive the claim exists to prevent. So a claim lasts at most one minute more than
    N actions' worst case plus N seconds, or the one-row budget if that is longer,
    and nothing in execution bounds a pin's route count, so nothing bounds the longest
    claim either (a pin of 50,000 routes: 76,337 minutes, about 53 days, at the
    defaults). A holder that dies mid-drive leaves the pin unclaimable for at most that
    long.
    """
    driver_actions = route_operations + 2
    sized = _next_whole_minute_after(
        _worst_case_ms(driver_actions, L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS)
    )
    return max(sized, claim_budget())


def unclaimed(model, now: datetime):
    """The "no other channel is driving this row" predicate, for RETRY-ONLY selects.

    True for a row that was never claimed and for one whose claim has expired. See
    the module docstring for why the reconcile's stale-build widening readers must
    NOT carry this predicate.
    """
    return or_(model.claimed_until.is_(None), model.claimed_until <= now)


async def claimable_row_ids(
    db: AsyncSession,
    model,
    row_ids: Iterable[uuid.UUID],
    now: datetime | None = None,
) -> set[uuid.UUID]:
    """Which of these rows are still FAILED and free to claim, RETRY-ONLY reader.

    The manual channel's pre-drive filter: it loads its FAILED rows through the
    unfiltered per-reservation readers (so it can still REPORT every row, including
    ones it will not drive), then asks this which of them it may actually claim.
    Rows missing from the answer are reported `in_progress` without a driver call,
    because an omitted row would read to the caller as already fixed.

    FOR UPDATE SKIP LOCKED, the health scheduler's `_due_rows` idiom: a row another
    channel is mid-claim on is skipped rather than waited on. SQLite ignores FOR
    UPDATE, so `claim_row`'s conditional UPDATE is the real race guard on every
    dialect and this is an optimization, not the correctness boundary.
    """
    ids = list(row_ids)
    if not ids:
        return set()
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(model.id)
        .where(model.id.in_(ids), model.status == "FAILED", unclaimed(model, now))
        .with_for_update(skip_locked=True)
    )
    return set(result.scalars().all())


class WiringRowClaims:
    """The per-row claims one retry pass took, and the rows it could not claim or write.

    One instance is created per retry pass and handed to the shared applies, which
    call `claim` immediately before each retried row's own driver call (an L3 pin
    once, before its switch's login, with a budget sized for its whole drive). `held` maps
    each row this pass claimed to the exact `claimed_until` stamp the claim wrote, so
    whether the pass STILL holds a row is judged by the database (the row's stamp is
    still this one), never by in-process belief: a claim that expired and was taken by
    another pass no longer matches (issue #1479). The caller reads `lost` afterwards to
    report those rows `in_progress` instead of `still_failed`: the row is not a
    failure, it is somebody else's work in flight. A row joins `lost` when its claim
    is lost and when a write this pass attempted on it is refused by
    `claim_write_clauses` (directly, or through `write_guard`).

    The ordinary consumer path never constructs one of these and never claims or
    checks a claim, so a reconcile can still write a row a retry pass holds (its
    key-based failure write and its stale park clear `claimed_until`); see
    `claim_write_clauses` for exactly what is and is not covered.
    """

    def __init__(self, budget: timedelta | None = None) -> None:
        self.budget = budget if budget is not None else claim_budget()
        self.held: dict[uuid.UUID, datetime] = {}
        self.lost: set[uuid.UUID] = set()

    async def claim(
        self,
        db: AsyncSession,
        model,
        row_id: uuid.UUID | None,
        budget: timedelta | None = None,
    ) -> bool:
        """Claim one row for this pass's driver call. True iff we may drive it.

        `row_id` None means the caller is not a retry channel (no row identity to
        claim), which is always drivable: the reconcile path. A row already claimed
        by THIS pass is drivable without a second UPDATE, so a row driven twice
        inside one apply (it cannot happen today, but nothing enforces that) never
        loses its own claim.

        `budget`, when given, replaces this pass's `self.budget` for this one claim:
        the stamp written is now plus `budget`. Only the L3 apply passes it, sized
        by `l3_pin_claim_budget` from the pin's drive sequence (issue #1482); the L1
        and L2 applies claim with `self.budget`. A row this pass already holds is
        answered True without a write, so a later, larger `budget` for it is not
        applied: its stamp stays the one the first claim wrote.
        """
        if row_id is None:
            return True
        if row_id in self.held:
            return True
        now = datetime.now(timezone.utc)
        stamp = now + (budget if budget is not None else self.budget)
        result = await db.execute(
            update(model)
            .where(
                model.id == row_id,
                model.status == "FAILED",
                unclaimed(model, now),
            )
            .values(claimed_until=stamp)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (result.rowcount or 0) == 0:
            self.lost.add(row_id)
            logger.info(
                "wiring retry: row %s of %s is claimed by another channel (or no longer "
                "FAILED); not driving it this pass (issue #817)",
                row_id,
                model.__tablename__,
            )
            return False
        self.held[row_id] = stamp
        return True


def claim_write_clauses(
    model,
    row_id: uuid.UUID,
    claims: WiringRowClaims | None,
    *,
    holder_or_unclaimed: bool = False,
) -> list:
    """The claim condition of a retry pass's failure writes and settles.

    Issue #1479: a failure write or a settle made by a retry pass (tick or manual) never
    changes or frees a row another pass holds, judged by the database. The pass hands
    over its ledger:

      - a row in `claims.held` is written only while its `claimed_until` still equals
        the stamp this pass's claim wrote (a claim that expired and was taken over by
        another pass no longer matches, so the stale holder is refused);
      - any other row, and every row when `claims` is None, is written only while it
        is unclaimed (`claimed_until` null or in the past).

    The failure writes by row id (`record_l1_failed`, `record_l2_failed`,
    `record_route_failed` given `row_id`) add these clauses to their own single
    `UPDATE`, so the check adds no query. The settles a retry pass makes on rows it
    loaded but never claimed (the stale parks, the supersession releases, the
    nil-allocation and unapplied-pin releases) go through `write_guard`, which locks
    the row under the same clauses first. A refused write changes nothing and adds the
    row to `claims.lost`.

    `holder_or_unclaimed` is the one exception, used only by the L3 holder's write
    after a switch whose route calls partly failed: those calls may have configured
    routes only that write records (#1001), so it is refused only while ANOTHER retry
    pass holds a live claim, and otherwise lands as on main even when a writer that
    does not claim (the reconcile, a teardown park) cleared this pass's stamp.

    Not covered, by design: the reconcile's writes (`claims` is never passed on that
    path), a retry holder's success and release flips (they are unconditional, as on
    main, issue #1481), and an ACTIVE row (a claim is only ever taken on a FAILED row,
    and every flip out of FAILED clears it, so no ACTIVE row is held).
    """
    now = datetime.now(timezone.utc)
    if claims is not None:
        stamp = claims.held.get(row_id)
        if stamp is not None and holder_or_unclaimed:
            return [or_(model.claimed_until == stamp, unclaimed(model, now))]
        if stamp is not None:
            return [model.claimed_until == stamp]
    return [unclaimed(model, now)]


@asynccontextmanager
async def write_guard(
    db: AsyncSession, model, row_id: uuid.UUID | None, claims: WiringRowClaims | None
) -> AsyncIterator[bool]:
    """Lock a row for a retry pass's settle, or refuse it; yields True iff it may write.

    With `claims` None (the reconcile) or no row identity this does nothing and yields
    True, so the reconcile's writes are untouched. Otherwise it runs `SELECT id ... WHERE
    id AND claim_write_clauses(...) FOR UPDATE` in `db`'s transaction: a match locks the
    row, so no other pass can claim it between this check and the write the caller
    makes in the same session; no match adds the row to `claims.lost` and yields False.
    The transaction is committed on exit, which releases the lock even when the body
    wrote nothing; on an exception it is rolled back and the exception propagates. The
    writer called inside must use `db` and must not roll back before it writes. SQLite
    ignores FOR UPDATE; there the check and the write simply run back to back on one
    connection.
    """
    if claims is None or row_id is None:
        yield True
        return
    found = (
        await db.execute(
            select(model.id)
            .where(model.id == row_id, *claim_write_clauses(model, row_id, claims))
            .with_for_update()
        )
    ).first()
    if found is None:
        claims.lost.add(row_id)
        logger.info(
            "wiring retry: write to row %s of %s refused: another pass holds its drive "
            "claim, or the row is gone (issue #1479)",
            row_id,
            model.__tablename__,
        )
    try:
        yield found is not None
    except BaseException:
        await db.rollback()
        raise
    await db.commit()
