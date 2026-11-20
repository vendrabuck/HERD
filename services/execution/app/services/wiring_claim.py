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

import enum
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)


def claim_budget() -> timedelta:
    """How long one claimed row's drive may take, as a whole number of minutes.

    Derived from the values that already bound a single row's worst-case drive; no
    new setting, deliberately (issue #817):

      - `nats_consumer.WIRING_DRIVER_ATTEMPTS` (3) sandbox attempts per driver
        action, each capped at `settings.execution_timeout_seconds` (30s) by
        `driver_sandbox.run_driver_action`.
      - Three driver ACTIONS per row: the per-switch `login`, the row's own
        connect/disconnect, add/remove or configure/remove_route op, and the
        per-switch `logout`. The login and logout are shared by every row on the
        switch, so charging all three to each row is deliberately generous.
      - The in-line backoff between attempts:
        `WIRING_DRIVER_INITIAL_DELAY` (0.2s) then doubled by
        `WIRING_DRIVER_BACKOFF_FACTOR`, capped at `WIRING_DRIVER_MAX_DELAY`, for
        `attempts - 1` sleeps per action.

    So the bound is attempts * actions * timeout + backoff, rounded UP to the next
    whole minute: 3 * 3 * 30s = 270s plus 1.8s of backoff, which rounds to 5
    minutes at the default timeout. Rounding up is what keeps the expiry safely
    clear of a drive that is still running, and the whole-minute figure matches the
    health scheduler's flat five-minute CLAIM_WINDOW, the same idiom one layer up.

    A deployment that raises `execution_timeout_seconds` gets a proportionally
    longer budget for free, which is the point of deriving it rather than pinning it.
    """
    from app.services.nats_consumer import (
        WIRING_DRIVER_ATTEMPTS,
        WIRING_DRIVER_BACKOFF_FACTOR,
        WIRING_DRIVER_INITIAL_DELAY,
        WIRING_DRIVER_MAX_DELAY,
    )

    actions_per_row = 3  # login, the row's own op, logout
    driver_seconds = (
        WIRING_DRIVER_ATTEMPTS * actions_per_row * max(1, settings.execution_timeout_seconds)
    )

    backoff_seconds = 0.0
    delay = WIRING_DRIVER_INITIAL_DELAY
    for _ in range(max(0, WIRING_DRIVER_ATTEMPTS - 1)):
        backoff_seconds += min(delay, WIRING_DRIVER_MAX_DELAY)
        delay *= WIRING_DRIVER_BACKOFF_FACTOR
    backoff_seconds *= actions_per_row

    total_seconds = driver_seconds + backoff_seconds
    whole_minutes = int(-(-total_seconds // 60))  # ceil, integer arithmetic
    return timedelta(minutes=max(1, whole_minutes))


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
    call `claim` immediately before each retried row's own driver call. `held` maps
    each row this pass claimed to the exact `claimed_until` stamp the claim wrote, so
    whether the pass STILL holds a row is judged by the database (the row's stamp is
    still this one), never by in-process belief: a claim that expired and was taken by
    another pass no longer matches (issue #1479). The caller reads `lost` afterwards to
    report those rows `in_progress` instead of `still_failed`: the row is not a
    failure, it is somebody else's work in flight. A row joins `lost` when its claim
    is lost, and when a write this pass attempted on it is refused because another
    retry pass holds a live claim on it (`note_refused_write`, `holder_record`).

    The ordinary consumer path never constructs one of these and never claims or
    checks a claim, so a reconcile can still write a row a retry pass holds (its
    key-based failure write and its stale park clear `claimed_until`); see
    `claim_write_clauses` for exactly what is and is not covered.
    """

    def __init__(self, budget: timedelta | None = None) -> None:
        self.budget = budget if budget is not None else claim_budget()
        self.held: dict[uuid.UUID, datetime] = {}
        self.lost: set[uuid.UUID] = set()

    async def claim(self, db: AsyncSession, model, row_id: uuid.UUID | None) -> bool:
        """Claim one row for this pass's driver call. True iff we may drive it.

        `row_id` None means the caller is not a retry channel (no row identity to
        claim), which is always drivable: the reconcile path. A row already claimed
        by THIS pass is drivable without a second UPDATE, so a row driven twice
        inside one apply (it cannot happen today, but nothing enforces that) never
        loses its own claim.
        """
        if row_id is None:
            return True
        if row_id in self.held:
            return True
        now = datetime.now(timezone.utc)
        stamp = now + self.budget
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


def claim_write_clauses(model, row_id: uuid.UUID, claims: WiringRowClaims | None) -> list:
    """The claim condition of a retry pass's failure write and of its settles.

    Issue #1479: a write made by a retry pass (tick or manual) never changes or frees a
    row another pass holds, judged by the database. The pass hands over its ledger:

      - a row in `claims.held` is written only while its `claimed_until` still equals
        the stamp this pass's claim wrote (a claim that expired and was taken over by
        another pass no longer matches, so the stale holder is refused);
      - any other row, and every row when `claims` is None, is written only while it
        is unclaimed (`claimed_until` null or in the past).

    Used by the failure writes by row id (`record_l1_failed`, `record_l2_failed`,
    `record_route_failed` given `row_id`), inside their own single `UPDATE`, and by
    `write_guard` for the settles a pass makes on rows it never claimed (the stale
    parks, the supersession releases, the nil-allocation and unapplied-pin releases).
    A holder's record of a driver action that SUCCEEDED does not use it: a successful
    action must reach the ledger even when its claim was cleared, see `holder_record`.

    Not covered, by design: the reconcile's writes (`claims` is never passed on that
    path), and an ACTIVE row (a claim is only ever taken on a FAILED row, and every
    flip out of FAILED clears it, so no ACTIVE row is held).
    """
    if claims is not None:
        stamp = claims.held.get(row_id)
        if stamp is not None:
            return [model.claimed_until == stamp]
    return [unclaimed(model, datetime.now(timezone.utc))]


async def note_refused_write(
    db: AsyncSession, model, row_id: uuid.UUID, claims: WiringRowClaims | None
) -> None:
    """After a refused retry write: report the row `in_progress` only if it is held.

    A refusal has three causes: another retry pass holds a live claim on the row (the
    only one where work is in flight, so the row joins `claims.lost` and reads back
    `in_progress`), the row left FAILED (the read-back reports its new status anyway),
    or the row's claim was cleared by a writer that does not claim (the reconcile, a
    teardown park), in which case nothing is running on it and it reads back by its
    own status, `still_failed` for a FAILED row. One short select, on refusal only.
    """
    if claims is None:
        return
    now = datetime.now(timezone.utc)
    live = (
        await db.execute(select(model.id).where(model.id == row_id, ~unclaimed(model, now)))
    ).first()
    if live is not None:
        claims.lost.add(row_id)


@asynccontextmanager
async def write_guard(
    db: AsyncSession, model, row_id: uuid.UUID | None, claims: WiringRowClaims | None
) -> AsyncIterator[bool]:
    """Lock a row for a retry pass's settle, or refuse it; yields True iff it may write.

    With `claims` None (the reconcile) or no row identity this does nothing and yields
    True, so the reconcile's writes are untouched. Otherwise it runs `SELECT id ... WHERE
    id AND claim_write_clauses(...) FOR UPDATE` in `db`'s transaction: a match locks the
    row, so no other pass can claim it between this check and the write the caller
    makes in the same session; no match yields False and, when another pass holds the
    row, adds it to `claims.lost` (`note_refused_write`). The transaction is committed on
    exit, which releases the lock even when the body wrote nothing; on an exception it
    is rolled back and the exception propagates. The writer called inside must use
    `db` and must not roll back before it writes. SQLite ignores FOR UPDATE; there the
    check and the write simply run back to back on one connection.
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
        await note_refused_write(db, model, row_id, claims)
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


# --- Recording a holder's driver action (issue #1479, round 3) ----------------
#
# A driver action that changed the device is never lost from the ledger. The holder of
# a row records the outcome of its own action through `holder_record`, which decides
# from the row as it is NOW (after whatever other writers did during the drive) how the
# hardware truth is recorded. Only another retry pass's LIVE claim makes the holder
# record nothing: that pass re-drives the row and records its own result.

#: The reason on a row parked because a holder's action may have left something on the
#: device after another writer had already settled the row toward release. Retryable
#: (no pinned prefix), so the release channels drive it.
SETTLED_ROW_PENDING_RELEASE = "applied after the row was settled for release; pending release"
#: The reason on a row reopened because a holder's removal landed while another writer
#: rebuilt the row ACTIVE: the device may no longer carry what the row says.
REBUILT_ROW_PENDING_BUILD = "removed after the row was rebuilt; pending rebuild"


class HolderRecord(enum.Enum):
    """How a holder records a driver action; see `decide_holder_record`."""

    WRITE = "write"
    REFUSE = "refuse"
    PARK = "park"
    REOPEN = "reopen"
    KEEP = "keep"


def decide_holder_record(
    *,
    exists: bool,
    mine: bool,
    held_by_other: bool,
    status: str | None,
    intended: str | None,
    frozen: bool,
    added: bool,
    succeeded: bool,
) -> HolderRecord:
    """The record a holder makes for one driver action, from the row as it is now.

    `added` is the action's possible effect on the device: True for a connect, a VLAN
    join, or a route configure (and for a failed route provision, which may have
    configured part of its set), False for their removals. `succeeded` is whether the
    driver confirmed it.

      - The row is gone, or still carries this pass's stamp: WRITE, the usual record.
      - Another retry pass holds a live claim: REFUSE; that pass re-drives the row and
        records its own result (the one case where this action is not recorded).
      - Otherwise the claim was cleared (by the reconcile or a teardown park) or has
        expired, and the record follows what the row now says:
          - FAILED and intended in this action's direction: WRITE.
          - an addition on an ACTIVE row: KEEP, the row already records the device as
            carrying it.
          - an addition on a row settled toward release (FAILED intended RELEASED, or
            RELEASED): a success on a frozen reservation is the writer's own record-time
            freeze branch (WRITE, issue #461), anything else is PARK, recorded FAILED
            intended RELEASED with what the action may have added, so the release
            channels remove it; never ACTIVE over a release decision.
          - a removal on an ACTIVE row (rebuilt meanwhile): REOPEN, FAILED intended
            ACTIVE with attempts 0, so the build channels re-apply it whichever of the
            two device calls landed last.
          - a removal on a RELEASED row: KEEP.
          - a removal on a FAILED intended ACTIVE row: on a frozen reservation a success
            is WRITE (released, the device no longer carries it); otherwise KEEP, the
            build channels re-apply it.
    """
    if not exists or mine:
        return HolderRecord.WRITE
    if held_by_other:
        return HolderRecord.REFUSE
    want = "ACTIVE" if added else "RELEASED"
    if status == "FAILED" and intended == want:
        return HolderRecord.WRITE
    if added:
        if status == "ACTIVE":
            return HolderRecord.KEEP
        return HolderRecord.WRITE if (frozen and succeeded) else HolderRecord.PARK
    if status == "ACTIVE":
        return HolderRecord.REOPEN
    if status == "RELEASED":
        return HolderRecord.KEEP
    return HolderRecord.WRITE if (frozen and succeeded) else HolderRecord.KEEP


@dataclass
class HolderPlan:
    """What `holder_record` decided, and the row it decided on (locked, or None)."""

    kind: HolderRecord
    row: object | None
    mine: bool


@asynccontextmanager
async def holder_record(
    db: AsyncSession,
    model,
    row_id: uuid.UUID | None,
    claims: WiringRowClaims | None,
    *,
    added: bool,
    succeeded: bool,
    lock_first=None,
) -> AsyncIterator[HolderPlan]:
    """Lock the holder's row and yield the plan for recording its driver action.

    With `claims` None (the reconcile) or no row identity this yields WRITE and touches
    nothing. Otherwise, in `db`'s transaction, it first locks the rows `lock_first`
    selects (the other rows the writer will touch, so every writer takes the locks in
    the same order: those rows, then the holder's row), then selects the holder's row
    `FOR UPDATE` with two flags computed in SQL (the row still carries this pass's
    stamp; another pass holds a live claim), reads the reservation's freeze, and
    yields `decide_holder_record`'s plan. REFUSE adds the row to `claims.lost`. The
    caller performs the plan in the same session; the transaction is committed on exit,
    releasing the locks even when nothing was written, and rolled back on an exception.
    """
    if claims is None or row_id is None:
        yield HolderPlan(HolderRecord.WRITE, None, False)
        return
    from app.services.l1_assignment_service import get_wiring_state

    if lock_first is not None:
        await db.execute(lock_first.with_for_update())
    now = datetime.now(timezone.utc)
    stamp = claims.held.get(row_id)
    mine_expr = (
        case((model.claimed_until == stamp, True), else_=False)
        if stamp is not None
        else literal(False)
    )
    held_expr = case((~unclaimed(model, now), True), else_=False)
    found = (
        await db.execute(
            select(model, mine_expr, held_expr).where(model.id == row_id).with_for_update()
        )
    ).first()
    if found is None:
        plan = HolderPlan(HolderRecord.WRITE, None, False)
    else:
        row, mine, held = found[0], bool(found[1]), bool(found[2])
        state = await get_wiring_state(db, row.reservation_id)
        kind = decide_holder_record(
            exists=True,
            mine=mine,
            held_by_other=held and not mine,
            status=row.status,
            intended=row.intended,
            frozen=state is not None and state.frozen,
            added=added,
            succeeded=succeeded,
        )
        plan = HolderPlan(kind, row, mine)
        if kind is HolderRecord.REFUSE:
            claims.lost.add(row_id)
        if kind is not HolderRecord.WRITE:
            logger.info(
                "wiring retry: holder of row %s of %s records %s (row is %s intended %s)",
                row_id,
                model.__tablename__,
                kind.value,
                row.status,
                row.intended,
            )
    try:
        yield plan
    except BaseException:
        await db.rollback()
        raise
    await db.commit()


async def reopen_rebuilt_row(db: AsyncSession, model, row_id: uuid.UUID) -> None:
    """REOPEN: a holder's removal landed on a row another writer rebuilt ACTIVE.

    The device may or may not still carry the row's content (the two device calls'
    order is unknown), so the row goes back to FAILED intended ACTIVE with attempts 0
    and a retryable reason, and the build channels re-apply it within one tick; a
    re-apply of what is already there is idempotent. The one write in the codebase
    that moves a row out of ACTIVE toward a rebuild; only `holder_record` asks for it.
    """
    await db.execute(
        update(model)
        .where(model.id == row_id, model.status == "ACTIVE")
        .values(
            status="FAILED",
            intended="ACTIVE",
            attempts=0,
            last_error=REBUILT_ROW_PENDING_BUILD,
            claimed_until=None,
        )
        .execution_options(synchronize_session=False)
    )
