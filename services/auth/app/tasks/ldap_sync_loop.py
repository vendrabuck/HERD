"""ADR 0011 phase 5: background loop that runs the LDAP group reconcile on an
interval, plus the ldap_sync_runs retention prune.

Mirrors services/reservations/app/tasks/expiration.py's shape: a forever loop,
per-cycle try/except Exception with logger.error(exc_info=True), and a flat
asyncio.sleep(interval) with no backoff. Started from main.py's lifespan only
when settings.auth_method == "ldap" AND settings.ldap_group_sync_enabled (the
ADR's "Scheduling and coordination" section); both are re-read from settings
on every call rather than captured once, so the gating helper stays cheap to
unit test.

The FIRST tick sleeps before syncing (decided: no boot-time sync burst on a
rolling restart, since every replica would otherwise fire a reconcile at
startup). Each tick calls ldap_sync_service.run_sync with trigger="interval",
which serializes through the module's existing _SyncSlot (the in-process lock
plus, on Postgres, the cross-replica advisory lock) exactly like sync-now and
run_sync's other direct callers; the module docstring at
ldap_sync_service.py:692-701 anticipates this third caller. SyncBusyError
(another replica or an overlapping sync-now already holds the slot) is
expected steady-state behavior, not a failure, so it is swallowed at
debug/info; any other exception is logged as an error and the loop continues
either way, since a tick must never kill the loop.

Retention prune runs in this SAME loop (decided: not the manual sync-now
path) after each sync attempt. last_prune seeds None, so the FIRST due tick
after startup prunes; after that, at most once per 24h, checked at tick
boundaries (the outbox relay's last_prune pattern,
herd_common/outbox.py, run_outbox_relay), so a short tick interval does not
turn the prune into a per-tick DELETE scan. A raising prune does NOT advance
last_prune: the cadence timer only moves forward on a SUCCESSFUL prune, so a
failed attempt retries on the very next tick rather than waiting out a full
24h with nothing pruned. A running row is never pruned: that status means
either a genuinely in-flight pass or a crashed one, and either way the row is
live audit state, not history. Because retention is enforced by this loop and
not by sync-now, a deployment that only ever runs sync-now accumulates
ldap_sync_runs rows indefinitely; that is a deliberate consequence of the ADR
decision to keep pruning out of the manual path, not an oversight.

Stale-run reap (issue #528) is NOT this loop's job and has no cadence gate
here: a hard process death mid-run (OOM kill, container crash, power loss)
never reaches ldap_sync_service.execute_run's finally block, so its row is
stuck at status "running" forever, exactly the row the prune above refuses
to touch. ldap_sync_service reaps those corpses at the START of every sync
run, whether triggered by this loop's tick or by sync-now, and does it
INSIDE the run-serialization slot (see run_sync / start_background_run), so
it can never fire against a run that is concurrently alive and holding the
slot. That covers both the interval deployment and a sync-now-only one,
which is why this loop has no reap call of its own.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app import database
from app.config import effective_ldap_sync_interval_seconds, settings
from app.models.ldap_sync_run import LdapSyncRun
from app.services import ldap_sync_service

logger = logging.getLogger(__name__)

# Retention prune cadence: independent of ldap_sync_interval_seconds, so a
# short interval (testing, or an aggressive deployment) does not turn the
# prune into a per-tick DELETE scan.
_PRUNE_INTERVAL = timedelta(hours=24)

# The PRODUCTION interval floor and the clamp that applies it both live in
# app.config (MIN_LDAP_SYNC_INTERVAL_SECONDS,
# effective_ldap_sync_interval_seconds), alongside the stale-run clamp built
# on top of them (effective_ldap_sync_run_stale_seconds, whose floor is twice
# the EFFECTIVE interval). ldap_group_sync_loop below stays unclamped so tests
# and the live-LDAP test can pass sub-second intervals directly; only the
# production call sites go through effective_interval_seconds.


def ldap_group_sync_loop_enabled() -> bool:
    """Whether main.py's lifespan should start the interval loop.

    Factored out of main.py so the gating decision is unit-testable without
    driving the whole lifespan context manager (both settings are consulted
    fresh, matching the ADR: "all new settings are consulted only when
    auth_method == 'ldap'").
    """
    return settings.auth_method == "ldap" and settings.ldap_group_sync_enabled


def effective_interval_seconds(raw: int) -> int:
    """Clamp a configured interval to a sane floor before starting the
    PRODUCTION loop (main.py's caller; the status endpoint reports the same
    number through this function).

    Delegates to app.config.effective_ldap_sync_interval_seconds, which owns
    the ONE implementation of this clamp, because the stale-run clamp
    (effective_ldap_sync_run_stale_seconds) floors at twice the EFFECTIVE
    interval and so has to apply the identical rule. Sharing the 60-second
    constant alone would not have been enough: two copies of "if raw < floor"
    can drift in what they do with it. This wrapper survives so the lifespan
    and the status router keep their existing entry point; the clamp warning
    is now emitted by app.config's logger, which is why the loop tests assert
    on the message rather than on the logger name.
    """
    return effective_ldap_sync_interval_seconds(raw)


def _utcnow() -> datetime:
    """Thin seam so tests can control elapsed time without patching the
    stdlib datetime module globally."""
    return datetime.now(timezone.utc)


async def _run_interval_tick() -> None:
    """One sync attempt with trigger="interval".

    SyncBusyError means the slot is already held (another replica, or an
    overlapping sync-now / previous tick that overran the interval); that is
    routine contention the run-serialization layer is designed to produce,
    never an error condition for this loop.
    """
    async with database.AsyncSessionLocal() as db:
        try:
            await ldap_sync_service.run_sync(db, trigger="interval")
        except ldap_sync_service.SyncBusyError as exc:
            logger.info(
                "LDAP interval sync skipped: busy (%s)",
                exc.reason,
                extra={"action": "ldap_sync_interval_skipped", "reason": exc.reason},
            )


async def _prune_old_runs() -> int:
    """Delete ldap_sync_runs rows started before the retention cutoff.

    "running" rows are excluded unconditionally regardless of age: a row in
    that status is either the run currently in flight or a crashed run that
    never reached its finally block, and either way it is live state an
    operator may still need to see, not prunable history.
    """
    cutoff = _utcnow() - timedelta(days=settings.ldap_sync_runs_retention_days)
    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            delete(LdapSyncRun).where(
                LdapSyncRun.started_at < cutoff,
                LdapSyncRun.status != "running",
            )
        )
        await db.commit()
        return result.rowcount or 0


async def ldap_sync_loop(interval_seconds: float) -> None:
    """Run LDAP interval syncs forever, with a cadence-limited retention prune.

    interval_seconds is REQUIRED and unclamped here: main.py's production
    call site passes effective_interval_seconds(settings.
    ldap_sync_interval_seconds) explicitly (matching the two sibling loops,
    expiration_loop and conversation_sweeper_loop, which also take their
    interval as an explicit argument), while tests and the live-LDAP test
    pass a small or sub-second value directly. The first tick sleeps BEFORE
    syncing (decision: no boot-time sync burst on a rolling restart).
    """
    logger.info(
        "LDAP sync interval loop started, interval=%ss",
        interval_seconds,
        extra={"action": "ldap_sync_loop_started", "interval_seconds": interval_seconds},
    )
    # None means "never pruned yet in this process": the FIRST due tick
    # prunes unconditionally, so restart-independent retention does not wait
    # out a full day after every rolling restart. From then on this only
    # advances on a SUCCESSFUL prune (see the try/except/else below).
    last_prune: datetime | None = None
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _run_interval_tick()
        except Exception:
            logger.error(
                "LDAP interval sync tick failed",
                exc_info=True,
                extra={"action": "ldap_sync_interval_tick_failed"},
            )

        now = _utcnow()
        if last_prune is None or now - last_prune >= _PRUNE_INTERVAL:
            try:
                removed = await _prune_old_runs()
            except Exception:
                # Deliberately does NOT advance last_prune: a failed prune
                # retries on the very next tick instead of waiting out a
                # full 24h with nothing pruned.
                logger.error(
                    "LDAP sync run retention prune failed",
                    exc_info=True,
                    extra={"action": "ldap_sync_runs_prune_failed"},
                )
            else:
                if removed:
                    logger.info(
                        "LDAP sync run retention prune removed %d row(s)",
                        removed,
                        extra={"action": "ldap_sync_runs_pruned", "removed": removed},
                    )
                last_prune = now
