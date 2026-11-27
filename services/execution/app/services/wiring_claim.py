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
  - `WiringRowClaims.claim` is a compare-and-swap: `UPDATE ... WHERE id AND status =
    'FAILED' AND (claimed_until IS NULL OR claimed_until <= now)`. No matching row
    means another channel holds the row (or a concurrent writer already moved it out
    of FAILED), so the caller drives nothing for it. The manual channel reports such a
    row as `in_progress`, the seventh retry outcome; the tick logs and skips it.
  - The same UPDATE also compares the direction (issue #1496). Each call site passes
    the direction it is about to drive (`intended`: ACTIVE for a connect, an add, or a
    provision; RELEASED for a disconnect, a remove, or a deprovision), and the stamp is
    written only where `direction_admits` holds: the row's `intended` equals that
    direction, or the direction is RELEASED and the row's reservation is frozen (a
    frozen reservation's wiring only ever leaves, so a release is admitted there
    whatever the row intends; a build never gets this exception).
    `SET claimed_until = CASE WHEN <admits> THEN :stamp ELSE claimed_until END ...
    RETURNING claimed_until`. A matching row the
    direction does not admit keeps every value (the UPDATE still runs, so on Postgres
    it writes a new row version and holds the row lock until its commit), and the
    returned stamp tells the two refusals apart from the same statement, with no second
    read: no row returned is "lost" (another holder, or no longer FAILED, read back
    `in_progress` when still FAILED), a row whose returned stamp is not this claim's is
    "wrong direction" (read back by its status, `still_failed` while FAILED).
  - A retry pass's failure write by row id on a row it does NOT hold (the failures
    recorded before any per-row claim: the switch read, the template read, the driver
    load, and the login on L1 and L2, L2's create_vlan pre-pass and re-allocation, and
    the switch read, template read, driver load, drive-gate refusal, and route-limit
    refusal on L3) takes
    the same `direction_admits` compare in its own UPDATE, because the row it planned
    may have been parked since: an admitted write lands as before; a refused one is
    rolled back, which is what leaves every value as it was, and joins
    `wrong_direction`. A write on a row the pass holds takes no compare (its claim
    compared already).
  - What this closes: a pass that planned a build from an older read can no longer
    claim and connect, add, or provision a row another pass parked (intended RELEASED)
    in between, nor undo that park with a failure it records before its claim; and a
    pass that planned a release can no longer claim a row whose intent became ACTIVE
    again on a reservation that is not frozen. What it does not close: the two windows
    issue #1481 tracks, in which the direction still matches (a retry pass that
    validated intent before a save and connects after the carried-delta apply passed
    over the row without parking it, and a retry planned from older content that
    drives it over a newer failed attempt). A refused row is retried by a later pass in
    the direction it then intends, with one exception, as on main: a FAILED
    intended-ACTIVE row on a frozen reservation is driven by no retry (the tick leaves
    it out as a frozen build, the manual retry reports it `frozen`). On a frozen
    reservation an admitted release drives the content the pass PLANNED: for L1 (one
    wire per row) that settles the row, but for L3 and L2, content a writer that does
    not compare direction added in the window (a newer route, a re-pointed allocation)
    is neither driven nor kept in the record, as on main; that is the stale-content
    window issue #1481 tracks. The frozen fact is read inside the statement and
    freezing is forward-only, so a freeze that commits after the statement's snapshot
    (for example while the claim waits on the row's lock) reads as not frozen and the
    release is refused like any other mismatch. That is safe for exclusion (it never
    drives), but on a frozen reservation the refused row is the unsettled case: it
    waits FAILED intended ACTIVE, and is settled only if the terminal teardown reads
    it after that (see `direction_admits`).

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

from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ClauseElement

from app.config import settings
from app.services.wiring_due import reservation_frozen

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
    N actions' worst case plus N seconds, or the one-row budget if that is longer.
    What bounds `route_operations` is the per-switch route limit (issue #1493,
    `herd_common.l3_route_limit.MAX_ROUTES_PER_SWITCH`, 1,000): one provision
    configures at most that many routes. Only a retry pass claims (the reconcile's own
    drives, deltas included, take no claim), and it drives a provision (the residue
    removals, then its route set) or a deprovision (the row's recorded routes). So at
    the defaults a retry provision that removes nothing holds its claim at most 1,530
    minutes (about 25.5 hours), and one that removes a residue of 1,000 routes and
    configures a disjoint 1,000 at most 3,057 minutes (about 51 hours). Removals are not
    limited, since refusing one would leave routes installed, so a claimed drive that
    removes more is bounded only by what it removes: a FAILED pin's recorded routes,
    removed first as a provision's residue or whole by a deprovision, exceed the limit
    when the pin was stored before the limit existed, and can grow past it, since each
    failed provision records its own set plus the removals that did not confirm. At
    50,000 route operations the
    budget is 76,337 minutes, about 53 days. A holder that dies mid-drive leaves the pin
    unclaimable for at most its budget.
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


def direction_admits(model, intended: str):
    """SQL: a retry pass may claim, or record a failure on, this row in `intended`.

    Issue #1496. True when the row's `intended` equals the direction, and, for the
    RELEASED direction only, also when the row's reservation is frozen: a frozen
    reservation's wiring only ever leaves (no retry drives its build rows), so a
    release refused there would strand the row. The BUILD direction never gets this
    exception.

    What an admitted frozen release drives is the content the pass planned from its
    earlier read, not the row's current content. For L1 (one wire per row) that settles
    the row. For L3 and L2 it does not cover content a writer that does not compare
    direction added in the window: a key-based failure write that recorded a newer
    route (L3) or re-pointed the allocation (L2) after the pass planned its release is
    neither driven nor kept in the record (the release flip, or an admitted release
    failure write with the pass's own routes, overwrites it), exactly as on main. That
    is the stale-content window issue #1481 tracks.

    The frozen fact is a correlated EXISTS over `reservation_wiring_state`
    (`wiring_due.reservation_frozen`), evaluated inside the caller's own statement,
    so no caller passes it and it is never older than that statement's snapshot.
    Freezing is forward-only (`freeze_reservation_wiring` is the only writer of
    `frozen` and only sets it true; `stamp_last_applied` never clears it, WIRE-ORDER-1,
    WIRE-FREEZE-1), so the one way the read can be out of date is a freeze that
    commits after that snapshot, including one that commits while the statement waits
    on the row's lock (the re-check after the wait sees the row's new version, not the
    new freeze). It reads as not frozen, and the release is refused like any other
    mismatch. That is safe for exclusion (it never drives), but on a frozen reservation
    a refusal is the unsettled case: the row is left FAILED intended ACTIVE, which no
    retry drives. The terminal teardown's read of FAILED rows parks and removes it on
    every layer (L3 since issue #1001, L1 and L2 since issue #1501), unless the flip
    to intended ACTIVE landed after that read, and except an L1 or L2 row with a
    pinned reason or under another pass's live claim, an L1 or L2 row whose key (and,
    on L2, allocation) also has an ACTIVE row, and an L2 row with a nil or dangling
    allocation on a port that has an ACTIVE row; and an L3 holder's
    partial-failure write can land over a pin the teardown parked (WIRE-TEARDOWN-6).
    A key-based build failure written after the freeze no longer makes that flip
    (issue #1501: it records intended RELEASED on a frozen reservation), so what
    remains is a flip by a writer whose statement read the reservation as not frozen.
    """
    if intended == "RELEASED":
        return or_(model.intended == "RELEASED", reservation_frozen(model))
    return model.intended == intended


def direction_guarded_values(model, values: dict, intended: str) -> dict:
    """Wrap each value of a row-id failure write in `CASE WHEN direction_admits THEN
    value ELSE column END` (issue #1496). Python values are bound with their column's
    type.

    What each part does. The CASE on `intended` is what tells an admitted write from a
    refused one inside the same statement: the writer returns `intended`, which equals
    the direction it records only when the write was admitted. What leaves a refused
    write's values as they were is the writer's rollback, before it returns. The CASE
    on the other columns is a second line behind that rollback; no test can tell it
    from a plain value while the rollback is there.
    """
    admits = direction_admits(model, intended)
    guarded = {}
    for name, value in values.items():
        column = getattr(model, name)
        if not isinstance(value, ClauseElement):
            value = literal(value, type_=column.type)
        guarded[name] = case((admits, value), else_=column)
    return guarded


def _same_instant(stored: datetime | None, stamp: datetime) -> bool:
    """True when a `claimed_until` read back from the database is `stamp`. SQLite
    drops the time zone a Postgres `timestamptz` keeps; both are UTC here."""
    if stored is None:
        return False
    if stored.tzinfo is None:
        return stored == stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stored == stamp


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

    `wrong_direction` (issue #1496) holds the rows whose claim, or whose failure write
    by row id on a row this pass does not hold, was refused because the direction is
    not admitted (`direction_admits`). Such a row is not another pass's work in flight,
    so it is kept out of `lost` and the pass reads it back by its status:
    `still_failed` while it is FAILED. A later pass retries it in the direction it then
    intends, except a FAILED intended-ACTIVE row on a frozen reservation, which no
    retry drives (as on main).

    The ordinary consumer path never constructs one of these and never claims or
    checks a claim, so a reconcile can still write a row a retry pass holds (its
    key-based failure write and its stale park clear `claimed_until`); see
    `claim_write_clauses` for exactly what is and is not covered.
    """

    def __init__(self, budget: timedelta | None = None) -> None:
        self.budget = budget if budget is not None else claim_budget()
        self.held: dict[uuid.UUID, datetime] = {}
        # The direction each held row was claimed for (issue #1496).
        self.held_direction: dict[uuid.UUID, str] = {}
        self.lost: set[uuid.UUID] = set()
        self.wrong_direction: set[uuid.UUID] = set()

    async def claim(
        self,
        db: AsyncSession,
        model,
        row_id: uuid.UUID | None,
        *,
        intended: str,
        budget: timedelta | None = None,
    ) -> bool:
        """Claim one row for this pass's driver call. True iff we may drive it.

        `intended` is the direction the call site is about to drive: "ACTIVE" for
        connect_ports, add_to_vlan, or an L3 provision; "RELEASED" for
        disconnect_ports, remove_from_vlan, or an L3 deprovision. The claim is one
        UPDATE matching the row id, status FAILED, and an unclaimed stamp, which
        writes the stamp only when `direction_admits(model, intended)` holds (the
        row's `intended` equals this direction, or this direction is RELEASED and the
        row's reservation is frozen) and returns the row's `claimed_until` (issue
        #1496). So:

          - no row returned: the claim is lost (another pass holds the row, or it is
            no longer FAILED, or it is gone); the row joins `lost`;
          - a row returned with a stamp other than this claim's: the direction is not
            admitted, no value on the row changes (the stamp is written back as it
            was), no driver call follows, and the row joins `wrong_direction`, not
            `lost`;
          - a row returned with this claim's stamp: the row joins `held`.

        The direction compare and the stamp are one statement, so no writer can
        change `intended` between them, and the classification comes from the same
        statement: there is no window in which the two refusals can be told apart
        wrongly.

        `row_id` None means the caller is not a retry channel (no row identity to
        claim), which is always drivable: the reconcile path. A row already claimed
        by THIS pass is answered without a second UPDATE, from the direction it was
        claimed for: the same direction is drivable, so a row driven twice inside one
        apply (it cannot happen today, but nothing enforces that) never loses its own
        claim; the other direction is refused into `wrong_direction` with no write.
        A pass reaches a held row in the other direction only when it loaded two FAILED
        rows of one reservation with the same key, one per direction: the retry maps
        the key to one row id, and the release-before-build apply reaches that id in
        both directions. The ledger's keyed writers update an existing non-RELEASED
        row for a key rather than insert a second, so that is not expected. The row's
        current `intended` is not re-read for a held row: the claim compared it
        atomically, and while this pass holds the row only a writer that never claims
        (the reconcile, issue #1481) can change it.

        `budget`, when given, replaces this pass's `self.budget` for this one claim:
        the stamp written is now plus `budget`. The L3 apply passes it, sized by
        `l3_pin_claim_budget` from the pin's drive sequence (issue #1482), and so does
        the L2 apply for an add_to_vlan, `claim_budget(ROW_DRIVER_ACTIONS + 1)`, for the
        undo a lost join may drive (issue #1413); the L1 apply and an L2 remove claim
        with `self.budget`. A row this pass already holds is
        answered without a write, so a later, larger `budget` for it is not
        applied: its stamp stays the one the first claim wrote.
        """
        if intended not in ("ACTIVE", "RELEASED"):
            raise ValueError(f"claim direction must be ACTIVE or RELEASED, got {intended!r}")
        if row_id is None:
            return True
        if row_id in self.held:
            if self.held_direction.get(row_id) == intended:
                return True
            self.refuse_wrong_direction(model, row_id, intended)
            return False
        now = datetime.now(timezone.utc)
        stamp = now + (budget if budget is not None else self.budget)
        result = await db.execute(
            update(model)
            .where(
                model.id == row_id,
                model.status == "FAILED",
                unclaimed(model, now),
            )
            .values(
                claimed_until=case(
                    (
                        direction_admits(model, intended),
                        literal(stamp, type_=model.claimed_until.type),
                    ),
                    else_=model.claimed_until,
                )
            )
            .returning(model.claimed_until)
            .execution_options(synchronize_session=False)
        )
        found = result.first()
        await db.commit()
        if found is None:
            self.lost.add(row_id)
            logger.info(
                "wiring retry: row %s of %s is claimed by another channel (or no longer "
                "FAILED); not driving it this pass (issue #817)",
                row_id,
                model.__tablename__,
            )
            return False
        # The row was unclaimed (null or a past stamp) and this stamp is in the future,
        # so the returned stamp equals this one exactly when the CASE wrote it.
        if not _same_instant(found[0], stamp):
            self.refuse_wrong_direction(model, row_id, intended)
            return False
        self.held[row_id] = stamp
        self.held_direction[row_id] = intended
        return True

    def refuse_wrong_direction(self, model, row_id: uuid.UUID, intended: str) -> None:
        """Record a claim or a failure write refused for its direction (issue #1496)."""
        self.wrong_direction.add(row_id)
        logger.info(
            "wiring retry: row %s of %s no longer intends %s; not driving it in that "
            "direction this pass (issue #1496)",
            row_id,
            model.__tablename__,
            intended,
        )


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
