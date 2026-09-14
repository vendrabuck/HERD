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
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
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
    """The per-row claims one retry pass took, and the rows it could not claim.

    One instance is created per retry pass and handed to the shared applies, which
    call `claim` immediately before each retried row's own driver call. The caller
    reads `lost` afterwards to report those rows `in_progress` instead of
    `still_failed`: the row is not a failure, it is somebody else's work in flight.

    The ordinary consumer path never constructs one of these and never claims: a
    reconcile drives the intended set, not a loaded FAILED row, and has no second
    channel to contend with.
    """

    def __init__(self, budget: timedelta | None = None) -> None:
        self.budget = budget if budget is not None else claim_budget()
        self.held: set[uuid.UUID] = set()
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
        result = await db.execute(
            update(model)
            .where(
                model.id == row_id,
                model.status == "FAILED",
                unclaimed(model, now),
            )
            .values(claimed_until=now + self.budget)
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
        self.held.add(row_id)
        return True
