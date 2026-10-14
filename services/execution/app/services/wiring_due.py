"""The background retry tick's selection predicate, in SQL (issue #1190).

`run_wiring_retry_tick` loads its candidates from three due queries (`due_failed_rows`,
`due_failed_l2_rows`, `due_failed_route_rows`), each "oldest FAILED rows under the
attempts cap, unclaimed, LIMIT batch", and then refuses to drive two kinds of row:

  - a row whose `last_error` starts with a pinned non-retryable reason
    (`wiring_retry_service.is_retryable_failure`, ADR 0007 Decision 5);
  - a BUILD-direction row (`intended != "RELEASED"`) whose reservation's wiring is
    frozen (`reservation_wiring_state.frozen`, the direction-scoped freeze of issue
    #369).

Neither refusal changes the row, so while those two tests ran only in Python, after
the LIMIT, such a row stayed FAILED, under the cap, and oldest. Once a ledger held a
batch of them, every batch was full of rows the tick would never drive and a newer
retryable row was never selected: the background retry for that ledger was dead.

This module is the same two rules as a WHERE clause, so the LIMIT counts no row they
set aside. The invariant, for every FAILED, under-cap, unclaimed row r:

    r is returned by its due query (ignoring LIMIT)
        if and only if
    is_retryable_failure(r.last_error)
        and not (r.intended != "RELEASED" and frozen(r.reservation_id))

Why each piece is written the way it is:

  - The reason test is `substr(last_error, 1, len(prefix)) != prefix`, never LIKE.
    `str.startswith` is case-sensitive. LIKE is case-sensitive on Postgres and
    case-insensitive for ASCII on SQLite, where the unit suite runs, so a LIKE
    predicate means one thing under the unit tests and another in production.
    `substr` and `=` compare code points the same way on both, and need no wildcard
    escaping.
  - NULL and the empty string are spelled out. `is_retryable_failure` treats both as
    retryable (a driver failure that recorded no message); under three-valued logic
    a bare `NOT (last_error ...)` is NULL for a NULL reason and would drop the row.
  - The prefixes are read from `nats_consumer.NON_RETRYABLE_REASON_PREFIXES` each
    time a statement is built, the tuple `is_retryable_failure` tests against, so
    the SQL cannot drift from the Python.
  - The frozen test is a correlated EXISTS over `reservation_wiring_state`, never a
    join: a reservation with no state row is not frozen, and an inner join would
    drop its rows. A subquery also keeps the due selects' FOR UPDATE on the ledger
    table alone.

What this does NOT promise (a known limit, by decision, issues #1190 and #1204). A
selected row can still end a tick unchanged for reasons these rules cannot see: any
upstream read that fails during a layer's retry (the intent from cabling's fork or from
inventory during the derivation, the switch or its template from inventory, and for L2
the fabric lookup), in either direction, raises TransientUpstreamError, and the rows it
covers are not driven and count no attempt. A build row whose intent cannot be read is
passed over on its own; a failed switch, template, or fabric read escapes the layer's
apply, so the tick drops the whole layer's selection for that tick (the layer's
rows_retried is 0 while rows_due counts it). While that upstream is down a batch of such
rows, older than everything else in a ledger, keeps newer rows of that ledger from being
selected. It heals when the upstream answers. Counting an attempt for an outage would
retire rows during the outage, so that is not done.

Who may use this. The three due queries take `tick_would_drive`, and
`count_tick_excluded` here reuses its two halves; nothing else. The per-reservation
FAILED readers (`failed_assignments_for_reservation` and its L2 and L3 siblings) are
shared with the manual retry, which REPORTS not-retryable and frozen rows, and with
the reconcile's stale-build widening, which must see every FAILED row (see
wiring_claim's module docstring); they deliberately do not take this predicate.

The tick keeps its two Python checks as a second line. A reservation can be frozen
between the select and the check; the row the check then skips is excluded here on
the next tick (a freeze is never undone), so it can take a batch slot at most once.
"""

from __future__ import annotations

from sqlalchemy import and_, case, func, literal, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reservation_wiring_state import ReservationWiringState


def _non_retryable_prefixes() -> tuple[str, ...]:
    """The pinned reason prefixes, read at call time from their one definition."""
    from app.services.nats_consumer import NON_RETRYABLE_REASON_PREFIXES

    return tuple(NON_RETRYABLE_REASON_PREFIXES)


def retryable_reason(model):
    """SQL for `is_retryable_failure(model.last_error)`, two-valued for every row.

    True for a NULL or empty reason, and for a reason that starts with none of the
    pinned prefixes. `substr(x, 1, n)` of a shorter text is the whole text, so a
    reason shorter than a prefix never equals it.
    """
    col = model.last_error
    starts_with_none = and_(
        true(),
        *[func.substr(col, 1, len(prefix)) != prefix for prefix in _non_retryable_prefixes()],
    )
    return or_(col.is_(None), col == "", starts_with_none)


def frozen_build(model):
    """SQL for "a BUILD-direction row whose reservation's wiring is frozen".

    The freeze is direction-scoped (issue #369): a release-direction row is never a
    frozen build, whatever its reservation's state. No state row means not frozen.
    """
    frozen_state = (
        select(literal(1))
        .where(
            ReservationWiringState.reservation_id == model.reservation_id,
            ReservationWiringState.frozen.is_(true()),
        )
        .exists()
    )
    return and_(model.intended != "RELEASED", frozen_state)


def tick_would_drive(model):
    """The retry tick's two skip rules as one predicate, for the three due queries only.

    True when neither rule sets the row aside. It does not mean the tick will make a
    driver call for the row: a build row is still revalidated against cabling's
    current intent first (see the module docstring's known limit).
    """
    return and_(retryable_reason(model), not_(frozen_build(model)))


async def count_tick_excluded(db: AsyncSession, model, max_attempts: int) -> tuple[int, int]:
    """How many FAILED, under-cap rows of one ledger the predicate excludes, by reason.

    Returns (not_retryable, frozen). The two are disjoint and follow the tick's own
    order: a row with a pinned reason counts as not retryable even when its
    reservation is frozen too. Claimed rows are counted: the claim is a separate,
    short-lived exclusion, and this is the standing population a long-lived ledger
    carries. One aggregate over the ledger's FAILED rows, no row is loaded or locked.
    """
    retryable = retryable_reason(model)
    result = await db.execute(
        select(
            func.coalesce(func.sum(case((not_(retryable), 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(retryable, frozen_build(model)), 1), else_=0)), 0),
        ).where(model.status == "FAILED", model.attempts < max_attempts)
    )
    not_retryable, frozen = result.one()
    return int(not_retryable), int(frozen)
