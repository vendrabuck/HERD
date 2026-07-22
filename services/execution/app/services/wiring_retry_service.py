"""Per-connection wiring retry: the manual and background reattempt of FAILED rows.

ADR 0007 Decision 6 items 2-3 (issue #345 P3b phase 4); direction-aware since ADR 0009
Decision 2 (issue #369). A hardware apply failure never rolls back the durable fork
save; it lands a FAILED l1_connection_assignments row (phase 3) tagged with the
direction (`intended`) the failed write was attempting. This module reattempts those
rows through the SAME driver machinery the consumer uses
(nats_consumer._apply_wiring_pairs), split by intended: an ACTIVE-intended row is
reattempted as a build (connect_ports, respecting the is_pair_active convergence gate
and the active-unique index, flipping the FAILED row to ACTIVE on success); a
RELEASED-intended row is reattempted as a release (disconnect_ports, respecting the
pair_needs_release idempotency gate, flipping the FAILED row to RELEASED on success).
Either way attempts/last_error accumulate on a repeat failure.

The freeze is direction-scoped (ADR 0009 phase 3, issue #369; vendra-approved
2026-07-22): a frozen reservation (ended: fork ARCHIVED, teardown ran) refuses only
BUILD-direction retries, since re-establishing a cross-connect for a dead reservation
is what the freeze exists to prevent. RELEASE-direction rows (intended RELEASED) are
still retryable while frozen: a disconnect that never confirmed leaves hardware
possibly still connected, and letting the terminal cleanup finish is exactly what a
frozen reservation wants. This is why the reservations proxy also permits retry for
COMPLETED/CANCELLED/FAILED and not just ACTIVE.

Two entry points share one core:

  - reattempt_reservation: the manual retry (the internal POST endpoint's worker).
    Reattempts every hardware-retryable FAILED row for one reservation regardless of
    the total-attempts cap (manual retry is the fallback for rows past the cap). On a
    frozen reservation it processes only RELEASE-direction rows and reports each
    BUILD-direction row with the outcome "frozen"; a frozen reservation with NO
    release-direction FAILED rows still raises WiringReservationFrozen (the pure-build
    refusal contract is unchanged).
  - run_wiring_retry_tick / run_wiring_retry_loop: the background auto-retry channel.
    Each tick sweeps at most wiring_retry_batch_size FAILED rows still under
    wiring_retry_max_attempts, skips a row only when its reservation is frozen AND the
    row is build-direction, and reattempts the survivors, batch capped exactly like
    the issue #24 health scheduler.

Both channels share one supersession guard (l1_assignment_service
supersede_release_if_reclaimed) run before a release retry fires the driver: a
release whose old cross-connect a newer build on another reservation already
destroyed is settled RELEASED with no driver call, reported as "superseded".

A FAILED row whose reason is one of the pinned verbatim-apply reasons (a recorded hop
that no longer resolves, or a hop set that is not a simple chain: ADR 0007 Decision 5)
is NOT hardware-retryable: the wiring intent itself cannot be applied and the recovery
is a fork re-save, so it is reported as such without burning a driver call.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.services.l1_assignment_service import (
    due_failed_rows,
    failed_assignments_for_reservation,
    get_wiring_state,
    supersede_release_if_reclaimed,
)
from app.services.nats_consumer import (
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_UNRESOLVABLE_REASON,
)

logger = logging.getLogger(__name__)

# The pinned non-retryable reason prefixes (ADR 0007 Decision 5). A FAILED row whose
# last_error starts with either one records an intent that a verbatim apply cannot
# realize (the recorded switch/port is gone, or the flattened hop set cannot be paired):
# a hardware retry would re-hit the same wall, so recovery is a fork re-save. The
# load-error variants ("recorded hop unresolvable: switch <id> not found", etc.) start
# with the same prefix and are non-retryable for the same reason: there is no resolvable
# driver to call.
_NON_RETRYABLE_PREFIXES = (WIRING_UNRESOLVABLE_REASON, WIRING_NOT_SIMPLE_CHAIN_REASON)


class WiringReservationFrozen(RuntimeError):
    """Raised when a build-only retry targets a reservation whose wiring is frozen.

    A frozen reservation has ended (fork ARCHIVED, teardown ran): reattempting a
    BUILD (connect_ports) would re-establish hardware for a dead reservation, so a
    frozen reservation with only build-direction FAILED rows refuses retry. The
    refusal is direction-scoped (ADR 0009 phase 3, issue #369): a frozen reservation
    that still has RELEASE-direction FAILED rows does NOT raise; its releases are
    processed and its build rows are reported "frozen". The internal endpoint maps
    this raise to 409 (ADR 0007 Decision 6/7).
    """


def is_retryable_failure(last_error: str | None) -> bool:
    """True when a FAILED row's reason is a transient/driver failure worth reattempting.

    False for the pinned verbatim-apply reasons (Decision 5): those name an
    unresolvable intent whose recovery is a re-save, not a driver retry. A None or
    empty reason is treated as retryable (a driver failure that recorded no message).
    """
    if not last_error:
        return True
    return not last_error.startswith(_NON_RETRYABLE_PREFIXES)


def _identity(row: L1ConnectionAssignment) -> dict:
    """The per-connection identity fields surfaced in a retry outcome."""
    return {
        "id": str(row.id),
        "switch_device_id": str(row.switch_device_id),
        "port_a": row.port_a,
        "port_b": row.port_b,
        "physical_connection_id": (
            str(row.physical_connection_id) if row.physical_connection_id else None
        ),
    }


def _outcome(row: L1ConnectionAssignment, outcome: str) -> dict:
    return {
        **_identity(row),
        "outcome": outcome,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
    }


async def _reattempt_rows(rows: list[L1ConnectionAssignment], get_db_session) -> list[dict]:
    """Reattempt a list of retryable FAILED rows in their own direction; return outcomes.

    Splits the rows by `intended` (issue #369, ADR 0009 Decision 2) before grouping by
    (reservation, switch): a row whose intended is ACTIVE is reattempted as a build
    (connect_ports), a row whose intended is RELEASED is reattempted as a release
    (disconnect_ports) - the direction fix this module exists for. Both directions are
    driven through the SAME consumer apply (_apply_wiring_pairs) per reservation, so a
    release-direction retry gets the same idempotency gate (pair_needs_release), driver
    machinery, and per-connection failure handling as the normal reconcile path. The
    rows may span reservations (the background batch does), so _apply_wiring_pairs is
    called once per reservation with both of that reservation's sets. On success a
    build flips the FAILED row to ACTIVE in place (record_l1_connect reuses the row); a
    release flips it to RELEASED in place (release_l1_connection reuses the row); on
    repeat failure record_l1_failed accumulates attempts and last_error. Outcomes are
    read back by row id: ACTIVE is "reconnected", RELEASED is "released", anything else
    is "still_failed". A row settled by the supersession guard reports "superseded".

    Before the release set is built, each release-direction row runs through the shared
    supersession guard (supersede_release_if_reclaimed): a release whose old
    cross-connect a newer build on another reservation already destroyed is flipped
    RELEASED with no driver call and reported "superseded", never entering the release
    set (ADR 0009 phase 3, issue #369). Both retry channels reach this function, so
    both get the guard.

    A TransientUpstreamError from resolving a switch (inventory/cabling 5xx) propagates
    to the caller: the manual endpoint maps it to 503; the background tick logs it and
    moves on. The rows stay FAILED for the next sweep.
    """
    from app.services.nats_consumer import _apply_wiring_pairs, _FetchContext

    if not rows:
        return []

    retry_ids: list[uuid.UUID] = [row.id for row in rows]

    # Supersession guard (shared by both channels): a release whose port a newer build
    # on another reservation already reclaimed needs no driver call. Flip it RELEASED
    # up front and keep its id so the read-back reports "superseded", not "released".
    superseded_ids: set[uuid.UUID] = set()
    async with get_db_session() as db:
        for row in rows:
            if row.intended == "RELEASED" and await supersede_release_if_reclaimed(db, row):
                superseded_ids.add(row.id)

    # reservation -> switch -> [(port_a, port_b, physical_connection_id)], split by
    # the row's intended direction. Superseded rows are already settled, so they are
    # left out of both apply sets.
    by_res_build: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}
    by_res_release: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}
    for row in rows:
        if row.id in superseded_ids:
            continue
        phys = str(row.physical_connection_id) if row.physical_connection_id else None
        res_str = str(row.reservation_id)
        target = by_res_release if row.intended == "RELEASED" else by_res_build
        target.setdefault(res_str, {}).setdefault(str(row.switch_device_id), []).append(
            (row.port_a, row.port_b, phys)
        )

    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)
        for res_str in {*by_res_build, *by_res_release}:
            build_by_switch = by_res_build.get(res_str, {})
            release_by_switch = by_res_release.get(res_str, {})
            await _apply_wiring_pairs(
                res_str, release_by_switch, build_by_switch, [], ctx, get_db_session
            )

    async with get_db_session() as db:
        refreshed = (
            (
                await db.execute(
                    select(L1ConnectionAssignment).where(L1ConnectionAssignment.id.in_(retry_ids))
                )
            )
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in refreshed}

    outcomes: list[dict] = []
    for rid in retry_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        if rid in superseded_ids:
            outcome = "superseded"
        elif row.status == "ACTIVE":
            outcome = "reconnected"
        elif row.status == "RELEASED":
            outcome = "released"
        else:
            outcome = "still_failed"
        outcomes.append(_outcome(row, outcome))
    return outcomes


async def reattempt_reservation(reservation_id: uuid.UUID | str, get_db_session) -> dict:
    """Manual retry: reattempt every hardware-retryable FAILED row of one reservation.

    The frozen refusal is direction-scoped (ADR 0009 phase 3, issue #369): on a frozen
    reservation only RELEASE-direction rows are processed, each BUILD-direction row is
    reported with the outcome "frozen", and a frozen reservation with NO
    release-direction FAILED rows still raises WiringReservationFrozen (the pure-build
    refusal contract is unchanged). Ignores the total-attempts cap by design: manual
    retry is exactly the fallback for a row parked past the cap. Non-retryable rows
    (pinned reasons) are reported without a driver call.

    Returns {"reservation_id", "results": [outcome, ...]} where each outcome carries the
    connection identity, the post-retry status/attempts/last_error, and an `outcome` of
    "reconnected" (a build succeeded), "released" (a release succeeded, issue #369),
    "superseded" (a release a newer build already made redundant, no driver call),
    "still_failed", "not_retryable", or "frozen" (a build refused on a frozen
    reservation).
    """
    res_str = str(reservation_id)
    async with get_db_session() as db:
        state = await get_wiring_state(db, reservation_id)
        frozen = state is not None and state.frozen
        failed = await failed_assignments_for_reservation(db, reservation_id)

    if frozen and not any(row.intended == "RELEASED" for row in failed):
        # Pure-build (or empty) frozen case: nothing to release, so retry is refused
        # exactly as before (the internal endpoint maps this to 409).
        raise WiringReservationFrozen(res_str)

    results: list[dict] = []
    retryable: list[L1ConnectionAssignment] = []
    for row in failed:
        if frozen and row.intended != "RELEASED":
            # A build cannot be reattempted for a dead reservation; report it without
            # a driver call while the release-direction rows below are still processed.
            results.append(_outcome(row, "frozen"))
        elif is_retryable_failure(row.last_error):
            retryable.append(row)
        else:
            results.append(_outcome(row, "not_retryable"))

    results.extend(await _reattempt_rows(retryable, get_db_session))
    return {"reservation_id": res_str, "results": results}


# --- Background auto-retry channel (Decision 6 item 2) -----------------------


async def run_wiring_retry_tick(get_db_session) -> dict:
    """One sweep of the background auto-retry channel; returns tick stats.

    Selects at most wiring_retry_batch_size FAILED rows still under
    wiring_retry_max_attempts (rows past the cap are manual-retry only), drops the
    non-hardware-retryable ones and any BUILD-direction row whose reservation is frozen,
    then reattempts the survivors. The frozen skip is row-scoped and direction-aware
    (ADR 0009 phase 3, issue #369): a RELEASE-direction row is retried even on a frozen
    reservation, so a stuck disconnect can still finish after the reservation ends. The
    batch cap bounds one tick exactly as the health scheduler's batch cap does; a repeat
    failure accumulates attempts, so a row converges toward the cap and eventually stops
    being swept.
    """
    batch = max(1, settings.wiring_retry_batch_size)
    max_attempts = settings.wiring_retry_max_attempts

    async with get_db_session() as db:
        due = await due_failed_rows(db, batch, max_attempts)

    stats = {
        "rows_due": len(due),
        "rows_retried": 0,
        "reconnected": 0,
        "released": 0,
        "superseded": 0,
        "still_failed": 0,
        "skipped_frozen": 0,
        "skipped_not_retryable": 0,
    }

    frozen_cache: dict[uuid.UUID, bool] = {}

    async def _is_frozen(res_id: uuid.UUID) -> bool:
        if res_id not in frozen_cache:
            async with get_db_session() as db:
                state = await get_wiring_state(db, res_id)
            frozen_cache[res_id] = bool(state is not None and state.frozen)
        return frozen_cache[res_id]

    to_retry: list[L1ConnectionAssignment] = []
    for row in due:
        if not is_retryable_failure(row.last_error):
            stats["skipped_not_retryable"] += 1
            continue
        # Direction-scoped freeze: only a BUILD-direction row is skipped on a frozen
        # reservation; a RELEASE-direction row is still retried (issue #369).
        if row.intended != "RELEASED" and await _is_frozen(row.reservation_id):
            stats["skipped_frozen"] += 1
            continue
        to_retry.append(row)

    if to_retry:
        try:
            outcomes = await _reattempt_rows(to_retry, get_db_session)
        except Exception:
            # A TransientUpstreamError (or any resolve failure) must never wedge the
            # loop: the rows stay FAILED and the next tick re-sweeps them.
            logger.warning("wiring retry tick: reattempt failed; rows stay FAILED", exc_info=True)
            outcomes = []
        stats["rows_retried"] = len(outcomes)
        for o in outcomes:
            if o["outcome"] == "reconnected":
                stats["reconnected"] += 1
            elif o["outcome"] == "released":
                stats["released"] += 1
            elif o["outcome"] == "superseded":
                stats["superseded"] += 1
            elif o["outcome"] == "still_failed":
                stats["still_failed"] += 1

    log = logger.info if stats["rows_due"] else logger.debug
    log("wiring_retry_tick", extra={"action": "wiring_retry_tick", **stats})
    return stats


async def run_wiring_retry_loop(get_db_session) -> None:
    """Long-running background auto-retry loop. Cancellable via task.cancel().

    Each tick runs run_wiring_retry_tick on the wiring_retry_interval_seconds cadence.
    A tick that raises (unexpected: the tick swallows reattempt failures) backs off
    exponentially up to a cap, exactly like the health scheduler loop; a healthy tick
    resets to the base interval.
    """
    base_interval = max(1, settings.wiring_retry_interval_seconds)
    max_backoff = max(base_interval * 10, 300)
    current_backoff = base_interval
    logger.info(
        "wiring retry channel started; interval=%ss batch=%s max_attempts=%s",
        base_interval,
        settings.wiring_retry_batch_size,
        settings.wiring_retry_max_attempts,
    )
    while True:
        tick_failed = False
        try:
            await run_wiring_retry_tick(get_db_session)
        except asyncio.CancelledError:
            logger.info("wiring retry channel cancelled; exiting loop")
            raise
        except Exception:
            logger.exception("wiring retry tick failed")
            tick_failed = True

        current_backoff = min(current_backoff * 2, max_backoff) if tick_failed else base_interval
        await asyncio.sleep(current_backoff)


def make_session_ctx_factory():
    """A get_db_session context factory over AsyncSessionLocal (consumer convention).

    Mirrors the NATS consumer's _get_db_session: `async with factory() as db` yields a
    fresh AsyncSession closed on exit. Used by the background loop, which has no request
    scope of its own.
    """
    from app.database import AsyncSessionLocal

    class _SessionCtx:
        async def __aenter__(self):
            self._session = AsyncSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _SessionCtx()

    return _get


async def start_wiring_retry_scheduler(app) -> None:
    """Start the auto-retry channel as a background task on the FastAPI app.

    Gated by settings.wiring_retry_enabled, mirroring the health scheduler's run-mode
    posture (issue #24): enabled by default so a poller-only replica runs it, and set
    false on API replicas to keep the background work on the poller fleet. Stored under
    app.state.wiring_retry_task so the shutdown hook can cancel and await it.
    """
    if not settings.wiring_retry_enabled:
        logger.info("wiring retry channel disabled; skipping startup")
        return
    get_db_session = make_session_ctx_factory()
    task = asyncio.create_task(run_wiring_retry_loop(get_db_session))

    def _surface_crash(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("wiring retry task exited unexpectedly: %s", exc)

    task.add_done_callback(_surface_crash)
    app.state.wiring_retry_task = task


async def stop_wiring_retry_scheduler(app) -> None:
    """Cancel the auto-retry channel task on app shutdown."""
    task = getattr(app.state, "wiring_retry_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
