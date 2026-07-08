"""Periodic device health-polling loop (ROADMAP #13 iter 1).

Reuses the driver login/status/logout sequence that on-demand
POST /device-check already runs. Each poll writes three ExecutionRun
rows (one per driver action) and updates a single device_health_status
row with the latest snapshot and the scheduler's bookkeeping.

The scheduler refreshes its "which devices to poll" registry from
inventory's /devices/health-config endpoint every
`health_poll_registry_refresh_seconds` (default 300), so adding a
poll_interval_seconds to a device or template takes effect within
roughly five minutes without thrashing inventory.

Race safety mirrors `inventory.apply_scheduler`:
SELECT ... FOR UPDATE SKIP LOCKED to fetch due rows, then a
conditional UPDATE that pushes next_poll_at forward by a five-minute
"claim window" before firing. If the poll crashes mid-flight, the
window expires and the row becomes due again. The claim doubles as the
multi-replica guard (issue #24): concurrent pollers against one schema
each win a disjoint set of rows, so poller replicas scale horizontally.

Fleet-scale bounds (issue #24): each tick claims at most
`health_poll_batch_size` due rows and runs at most
`health_poll_max_concurrency` polls (driver subprocesses) at once.
Devices carry a persisted polling tier ("idle" or "in_use") flipped by
the reservation lifecycle events the NATS consumer already processes;
`health_poll_in_use_interval_seconds` / `health_poll_idle_interval_seconds`
override the registry-resolved interval per tier, with 0 (the default)
meaning no override.

This module never raises out of the loop; a poll that crashes is
logged and the row resweeps on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from herd_common.outbox import enqueue_event
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.device_health_status import DeviceHealthStatus
from app.models.outbox import OutboxEvent
from app.services.execution_service import fetch_device, fetch_template, run_driver_action

# ROADMAP #13 iter 2: NATS stream + subject for health-transition events.
# Publisher mirrors the inline `_publish_nats_event` helper from
# reservations (services/reservations/app/services/reservation_service.py).
HEALTH_NATS_SUBJECT = "herd.health.status_changed"

logger = logging.getLogger(__name__)

# A system-poll sentinel: scheduled polls have no real user, but
# ExecutionRun.user_id is NOT NULL. The all-zero UUID is unambiguous
# and easy to filter out of audit views.
SYSTEM_POLL_USER_ID = uuid.UUID(int=0)

# Five-minute claim window: when we claim a due row, push next_poll_at
# forward by this much before firing. If the poll crashes, the row
# becomes due again after the window expires.
CLAIM_WINDOW = timedelta(minutes=5)

# Polling tiers (issue #24). A device moves to IN_USE on reservation
# created/updated and back to IDLE on completed/cancelled/failed, driven
# by the reservation lifecycle events this service already consumes.
TIER_IDLE = "idle"
TIER_IN_USE = "in_use"

# Which consumed reservation events flip a reservation's devices to which
# tier. reservation.updated is handled separately (post-edit device set to
# in-use, removed devices to idle), so it appears in neither set.
TIER_IN_USE_EVENTS = {"reservation.created"}
TIER_IDLE_EVENTS = {
    "reservation.completed",
    "reservation.cancelled",
    "reservation.failed",
}


# --- Registry: which devices to poll ---


# Module-level cache. The loop refreshes this; readers (fire_poll) read
# it under no lock since dict reads/writes are atomic in CPython.
_registry: dict[uuid.UUID, int] = {}
_registry_last_refresh: datetime | None = None


async def _fetch_registry(client: httpx.AsyncClient) -> dict[uuid.UUID, int] | None:
    """Fetch the device-id -> resolved-interval map from inventory.

    Returns None on any failure so callers can leave the existing cache
    in place. Closed-default beats wiping the registry on a transient
    inventory blip.
    """
    if not settings.internal_api_token:
        logger.warning("health scheduler: INTERNAL_API_TOKEN unset; cannot fetch registry")
        return None
    url = f"{settings.inventory_service_url.rstrip('/')}/devices/health-config"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("health scheduler: registry fetch failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("health scheduler: registry fetch %d %s", resp.status_code, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("health scheduler: registry returned malformed JSON")
        return None
    out: dict[uuid.UUID, int] = {}
    for row in data:
        try:
            out[uuid.UUID(row["device_id"])] = int(row["resolved_interval_seconds"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def _refresh_registry_if_due(
    client: httpx.AsyncClient,
    now: datetime,
    db: AsyncSession,
) -> None:
    """Refresh the cached registry on the configured cadence + seed missing rows.

    Updates _registry in place. Any device newly present in the registry
    without a device_health_status row gets a stub inserted with
    last_status='UNKNOWN' and next_poll_at=now, so it polls on the next tick.
    """
    global _registry_last_refresh
    refresh_seconds = settings.health_poll_registry_refresh_seconds
    if (
        _registry_last_refresh is not None
        and (now - _registry_last_refresh).total_seconds() < refresh_seconds
    ):
        return
    fetched = await _fetch_registry(client)
    if fetched is None:
        return
    _registry.clear()
    _registry.update(fetched)
    _registry_last_refresh = now
    await _seed_missing_status_rows(db, fetched.keys(), now)


async def _seed_missing_status_rows(
    db: AsyncSession,
    device_ids,
    now: datetime,
) -> None:
    """Insert stub device_health_status rows for any device not yet tracked.

    Uses Postgres ON CONFLICT DO NOTHING. On SQLite (test backend) the
    pg_insert form falls back to a plain INSERT and we catch IntegrityError
    per row instead. Either way, the contract is "this device now has a row
    that is immediately due to poll."
    """
    if not device_ids:
        return
    try:
        await db.execute(
            pg_insert(DeviceHealthStatus)
            .values([{"device_id": did, "next_poll_at": now} for did in device_ids])
            .on_conflict_do_nothing(index_elements=["device_id"])
        )
        await db.commit()
        return
    except Exception:
        # SQLite path: fall back to one-by-one insert with IntegrityError swallow.
        await db.rollback()
    for did in device_ids:
        try:
            db.add(DeviceHealthStatus(device_id=did, next_poll_at=now))
            await db.commit()
        except IntegrityError:
            await db.rollback()
        except Exception:
            await db.rollback()
            logger.exception("seed_missing_status_rows: failed for %s", did)


# --- Tick: pick due rows, claim, fire ---


async def _due_rows(
    db: AsyncSession, now: datetime, limit: int | None = None
) -> list[DeviceHealthStatus]:
    """SELECT ... FOR UPDATE SKIP LOCKED for due polling rows.

    Same pattern as inventory.apply_scheduler._due_jobs. SQLite ignores
    FOR UPDATE; the conditional UPDATE in _claim_row is the actual
    race-safety guard. The LIMIT (health_poll_batch_size, issue #24) bounds
    how many rows one tick even considers; rows past it stay due for later
    ticks or for another poller replica.
    """
    if limit is None:
        limit = settings.health_poll_batch_size
    rows = await db.execute(
        select(DeviceHealthStatus)
        .where(DeviceHealthStatus.next_poll_at <= now)
        .order_by(DeviceHealthStatus.next_poll_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars().all())


async def _due_count(db: AsyncSession, now: datetime) -> int:
    """Total due rows, unbounded by the batch limit (tick observability)."""
    result = await db.execute(
        select(func.count())
        .select_from(DeviceHealthStatus)
        .where(DeviceHealthStatus.next_poll_at <= now)
    )
    return int(result.scalar() or 0)


async def _claim_row(db: AsyncSession, device_id: uuid.UUID, now: datetime) -> bool:
    """Push next_poll_at by the claim window. Returns True iff we won the race.

    Conditional update: only succeed if next_poll_at is still in the
    past. If another scheduler beat us, rowcount is 0 and we skip the
    poll. The claim is the source of truth for "this scheduler owns this
    row for the duration of the poll."
    """
    claim_until = now + CLAIM_WINDOW
    result = await db.execute(
        update(DeviceHealthStatus)
        .where(
            DeviceHealthStatus.device_id == device_id,
            DeviceHealthStatus.next_poll_at <= now,
        )
        .values(next_poll_at=claim_until)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) > 0


# --- Tiered intervals (issue #24) ---


def _effective_interval(registry_interval: int, tier: str | None) -> int:
    """Resolve the poll interval for a device given its tier.

    A tier override of 0 (the default) means "no override": the
    registry-resolved interval (device or template poll_interval_seconds)
    applies unchanged, which is exactly the pre-tier behavior. An unknown or
    NULL tier is treated as idle, matching the column default.
    """
    if tier == TIER_IN_USE and settings.health_poll_in_use_interval_seconds > 0:
        return settings.health_poll_in_use_interval_seconds
    if tier != TIER_IN_USE and settings.health_poll_idle_interval_seconds > 0:
        return settings.health_poll_idle_interval_seconds
    return registry_interval


async def apply_tier_transition(session_factory, device_ids, tier: str) -> None:
    """Move a set of devices to a polling tier, persisted on their status rows.

    Moving to in-use with a configured in-use interval also pulls next_poll_at
    earlier (never later) so the faster cadence starts now rather than after
    the current idle interval elapses. Moving to idle never touches
    next_poll_at: the slower cadence takes effect when the next poll
    reschedules. A device with no status row yet (reserved before its first
    registry seed) gets one inserted for the in-use transition so it does not
    idle through the whole reservation; for idle there is nothing to record
    since idle is the column default.

    `session_factory` accepts either an async_sessionmaker or the NATS
    consumer's get_db_session convention; both yield a session via
    `async with session_factory() as db`.
    """
    if not device_ids:
        return
    ids: list[uuid.UUID] = []
    for did in device_ids:
        try:
            ids.append(uuid.UUID(str(did)))
        except (ValueError, TypeError):
            continue
    if not ids:
        return
    now = datetime.now(timezone.utc)
    values: dict = {"poll_tier": tier}
    if tier == TIER_IN_USE and settings.health_poll_in_use_interval_seconds > 0:
        target = now + timedelta(seconds=settings.health_poll_in_use_interval_seconds)
        values["next_poll_at"] = case(
            (DeviceHealthStatus.next_poll_at > target, target),
            else_=DeviceHealthStatus.next_poll_at,
        )
    async with session_factory() as db:
        await db.execute(
            update(DeviceHealthStatus)
            .where(DeviceHealthStatus.device_id.in_(ids))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if tier != TIER_IN_USE:
            return
        existing = await db.execute(
            select(DeviceHealthStatus.device_id).where(DeviceHealthStatus.device_id.in_(ids))
        )
        have = set(existing.scalars().all())
        for did in ids:
            if did in have:
                continue
            try:
                db.add(DeviceHealthStatus(device_id=did, next_poll_at=now, poll_tier=tier))
                await db.commit()
            except IntegrityError:
                await db.rollback()


async def apply_reservation_event_tiers(session_factory, event_type: str, event_data: dict) -> None:
    """Map one consumed reservation lifecycle event onto tier transitions.

    created moves the reservation's devices to in-use; completed, cancelled,
    and failed move them back to idle (conflict detection guarantees a device
    is in at most one active reservation, so idling the whole set is safe).
    updated moves the post-edit device set to in-use and the removed devices
    to idle. Every transition is an absolute UPDATE, so a redelivered event
    re-applies the same tier idempotently.
    """
    if event_type == "reservation.updated":
        await apply_tier_transition(session_factory, event_data.get("device_ids", []), TIER_IN_USE)
        await apply_tier_transition(
            session_factory, event_data.get("removed_device_ids", []), TIER_IDLE
        )
    elif event_type in TIER_IN_USE_EVENTS:
        await apply_tier_transition(session_factory, event_data.get("device_ids", []), TIER_IN_USE)
    elif event_type in TIER_IDLE_EVENTS:
        await apply_tier_transition(session_factory, event_data.get("device_ids", []), TIER_IDLE)


def _next_poll_at(
    interval_seconds: int,
    consecutive_failures: int,
    now: datetime,
) -> datetime:
    """Compute next_poll_at after a poll. Backoff kicks in past the threshold.

    Success and failures within threshold both schedule the next poll
    at now + interval. Failures past the threshold scale up with a
    bounded exponential plus jitter so an UNREACHABLE device does not
    generate a flood of ExecutionRun rows.
    """
    threshold = settings.health_poll_max_consecutive_failures
    if consecutive_failures <= threshold:
        return now + timedelta(seconds=interval_seconds)
    overflow = consecutive_failures - threshold
    # 2 ** overflow scales fast; cap then add jitter up to half the interval.
    raw = interval_seconds * (2**overflow)
    capped = min(raw, settings.health_poll_backoff_cap_seconds)
    jitter = random.uniform(0, interval_seconds / 2)
    return now + timedelta(seconds=capped + jitter)


def _decide_transition(
    *,
    old_failures: int,
    new_failures: int,
    threshold: int,
) -> str | None:
    """Return 'bad_news', 'recovery', or None.

    Emit-on-Nth-failure dedupe: bad_news fires only when failures cross
    the threshold from below (so a flap that never accumulates `threshold`
    consecutive failures is silent). Recovery fires only when failures
    reset from non-zero to zero.

    Edge case: if `health_poll_max_consecutive_failures` is lowered at
    runtime past a device that is already over it, no re-fire happens.
    Correct dedupe behavior.
    """
    if old_failures < threshold <= new_failures:
        return "bad_news"
    if old_failures > 0 and new_failures == 0:
        return "recovery"
    return None


async def fire_poll(
    session_factory: async_sessionmaker[AsyncSession],
    device_id: uuid.UUID,
    interval_seconds: int,
    nc=None,
) -> None:
    """Run login/status/logout and update the health-status row.

    Each poll runs in a fresh session so the outer "due rows" session's
    locks are released between devices (matches apply_scheduler).
    Logout failure does not mask the real outcome.

    If iter 2's notify path is enabled and the new consecutive_failures
    count crosses the bad-news or recovery threshold, a health-transition
    event (subject `herd.health.status_changed`) is staged into the outbox
    in the SAME transaction as the status update, so the event exists iff
    the status change committed (issue #21). The relay (main.py) publishes
    it to NATS. `nc` is retained for call-site compatibility but is unused:
    publishing is now the relay's job, not fire_poll's.
    """
    started = datetime.now(timezone.utc)
    last_status = "UNKNOWN"
    last_run_id: uuid.UUID | None = None
    poll_failed = False

    try:
        device_data = await fetch_device(device_id)
        template_data = await fetch_template(device_data["template_id"])
    except Exception as exc:
        logger.warning(
            "health_poll_failed",
            extra={
                "action": "health_poll_failed",
                "device_id": str(device_id),
                "error": f"fetch failed: {exc}",
            },
        )
        last_status = "UNREACHABLE"
        poll_failed = True
        device_data = None
        template_data = None

    if device_data and template_data:
        async with session_factory() as run_session:
            login_run = await run_driver_action(
                run_session, device_data, template_data, "login", SYSTEM_POLL_USER_ID
            )
            last_run_id = login_run.id
            if login_run.status != "SUCCESS":
                last_status = "UNREACHABLE"
                poll_failed = True
            else:
                status_run = await run_driver_action(
                    run_session, device_data, template_data, "status", SYSTEM_POLL_USER_ID
                )
                last_run_id = status_run.id
                if status_run.status == "SUCCESS":
                    last_status = "HEALTHY"
                else:
                    last_status = "DEGRADED"
                    poll_failed = True
                # Logout best-effort: failure here is recorded as a separate
                # ExecutionRun but does not mask the login/status outcome.
                try:
                    await run_driver_action(
                        run_session,
                        device_data,
                        template_data,
                        "logout",
                        SYSTEM_POLL_USER_ID,
                    )
                except Exception:
                    logger.warning(
                        "health_poll: logout raised for device %s; outcome stays %s",
                        device_id,
                        last_status,
                    )

    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    # Update the health-status row in a fresh session so the run-session
    # commits do not interfere. Capture the pre-update state so the
    # transition check (iter 2) can fire only on threshold crossings, and
    # stage any health-transition event into the outbox BEFORE this session
    # commits, so the event row commits atomically with the status update
    # (issue #21). The relay (main.py) publishes the staged row to NATS.
    async with session_factory() as status_session:
        row = await status_session.get(DeviceHealthStatus, device_id)
        if row is None:
            # Race: seed step missed this device. Create a row now.
            row = DeviceHealthStatus(device_id=device_id, next_poll_at=datetime.now(timezone.utc))
            status_session.add(row)
            await status_session.flush()
        old_failures = row.consecutive_failures or 0
        old_status = row.last_status or "UNKNOWN"
        if poll_failed:
            row.consecutive_failures = old_failures + 1
        else:
            row.consecutive_failures = 0
        row.last_status = last_status
        row.last_run_id = last_run_id
        row.last_polled_at = datetime.now(timezone.utc)
        row.next_poll_at = _next_poll_at(
            interval_seconds, row.consecutive_failures, datetime.now(timezone.utc)
        )
        new_failures = row.consecutive_failures

        # ROADMAP #13 iter 2 + issue #21: stage a health-transition event on a
        # bad-news / recovery transition into the outbox, in the SAME
        # transaction as the status update. The bad_news threshold is the same
        # value that controls the failure backoff, so the event aligns with the
        # scheduler's existing "device is in trouble" signal; recovery fires on
        # the first poll that resets failures to zero. The
        # health_poll_notify_enabled flag still gates whether any event is
        # emitted at all.
        if settings.health_poll_notify_enabled:
            transition = _decide_transition(
                old_failures=old_failures,
                new_failures=new_failures,
                threshold=settings.health_poll_max_consecutive_failures,
            )
            if transition is not None:
                device_name = device_data.get("name") if device_data else None
                payload = {
                    "event": "device.health_transition",
                    "device_id": str(device_id),
                    "device_name": device_name or str(device_id),
                    "old_status": old_status,
                    "new_status": last_status,
                    "transition_kind": transition,
                    "consecutive_failures": new_failures,
                    "last_run_id": str(last_run_id) if last_run_id else None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                enqueue_event(status_session, OutboxEvent, HEALTH_NATS_SUBJECT, payload)

        await status_session.commit()

    log_event = "health_poll_completed" if not poll_failed else "health_poll_failed"
    logger.info(
        log_event,
        extra={
            "action": log_event,
            "device_id": str(device_id),
            "last_status": last_status,
            "duration_ms": duration_ms,
        },
    )


# --- Tick and loop ---


async def run_tick(
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient | None,
    nc=None,
) -> dict:
    """One scheduler tick: refresh registry if due, claim, fire, report.

    At most `health_poll_batch_size` due rows are considered and at most
    `health_poll_max_concurrency` polls run at once (issue #24). The claim is
    taken only AFTER the semaphore is acquired, so a row never sits claimed
    while waiting for a slot: the five-minute CLAIM_WINDOW only has to outlive
    one in-flight poll (bounded by the per-action driver timeouts, well under
    the window), and a peer poller replica that claims a still-waiting row
    simply wins the conditional update and this replica skips it. The tick
    awaits every poll before returning, so a single replica never overlaps
    its own ticks.

    Returns the tick stats that are also emitted as the `health_tick`
    structured log: rows_due (total due, unbounded by the batch),
    rows_claimed, polls_fired, and polls_deferred (polls that found the
    semaphore full and had to wait).
    """
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await _refresh_registry_if_due(client, now, db)

    stats = {"rows_due": 0, "rows_claimed": 0, "polls_fired": 0, "polls_deferred": 0}
    to_fire: list[tuple[uuid.UUID, int]] = []
    async with session_factory() as db:
        stats["rows_due"] = await _due_count(db, now)
        due = await _due_rows(db, now)
        for row in due:
            interval = _registry.get(row.device_id)
            if interval is None:
                # Device dropped from registry; don't poll it. Push
                # next_poll_at far enough out that the row doesn't
                # reappear every tick.
                row.next_poll_at = now + timedelta(
                    seconds=settings.health_poll_registry_refresh_seconds
                )
                await db.commit()
                continue
            to_fire.append((row.device_id, _effective_interval(interval, row.poll_tier)))

    sem = asyncio.Semaphore(max(1, settings.health_poll_max_concurrency))

    async def _claim_and_fire(device_id: uuid.UUID, interval: int) -> None:
        if sem.locked():
            stats["polls_deferred"] += 1
        async with sem:
            async with session_factory() as db:
                claimed = await _claim_row(db, device_id, now)
            if not claimed:
                return
            stats["rows_claimed"] += 1
            stats["polls_fired"] += 1
            try:
                await fire_poll(session_factory, device_id, interval, nc=nc)
            except Exception:
                logger.exception("fire_poll crashed for device %s", device_id)

    if to_fire:
        await asyncio.gather(*(_claim_and_fire(did, interval) for did, interval in to_fire))

    log = logger.info if stats["rows_due"] else logger.debug
    log(
        "health_tick",
        extra={
            "action": "health_tick",
            "rows_due": stats["rows_due"],
            "rows_claimed": stats["rows_claimed"],
            "polls_fired": stats["polls_fired"],
            "polls_deferred": stats["polls_deferred"],
        },
    )
    return stats


async def run_health_scheduler_loop(
    session_factory: async_sessionmaker[AsyncSession],
    nc=None,
) -> None:
    """Long-running poll loop. Cancellable via task.cancel().

    Each tick refreshes the registry if due, then claims up to
    `health_poll_batch_size` due rows and fires them under the
    `health_poll_max_concurrency` semaphore (see run_tick). Tick-level
    failures (e.g. DB unreachable) back off exponentially up to a cap; a
    healthy tick resets to the base interval.

    `nc` is the NATS client used to publish health-transition events
    (iter 2). Pass None to disable publishing without changing the
    `health_poll_notify_enabled` config flag.
    """
    base_interval = settings.health_poll_scheduler_tick_seconds
    max_backoff = max(base_interval * 10, 300)
    current_backoff = base_interval
    logger.info(
        "health scheduler started; tick=%ss refresh=%ss batch=%s concurrency=%s",
        base_interval,
        settings.health_poll_registry_refresh_seconds,
        settings.health_poll_batch_size,
        settings.health_poll_max_concurrency,
    )
    while True:
        tick_failed = False
        try:
            async with httpx.AsyncClient() as client:
                await run_tick(session_factory, client, nc=nc)
        except asyncio.CancelledError:
            logger.info("health scheduler cancelled; exiting loop")
            raise
        except Exception:
            logger.exception("health scheduler tick failed")
            tick_failed = True

        current_backoff = min(current_backoff * 2, max_backoff) if tick_failed else base_interval
        try:
            await asyncio.sleep(current_backoff)
        except asyncio.CancelledError:
            raise


# --- Lifespan helpers ---


async def start_health_scheduler(app) -> None:
    """Start the scheduler as a background task on the FastAPI app.

    Stored under app.state.health_scheduler_task so the shutdown hook
    can cancel and await it. Reads `app.state.nats` (set by
    `start_nats_consumer` which runs first in main.py lifespan) to
    publish iter-2 transition events. If NATS connect failed earlier
    the scheduler still works; transition events just get dropped.
    """
    if not settings.health_poll_scheduler_enabled:
        logger.info("health scheduler disabled; skipping startup")
        return
    from app.database import AsyncSessionLocal

    nc = getattr(app.state, "nats", None)
    task = asyncio.create_task(run_health_scheduler_loop(AsyncSessionLocal, nc=nc))

    def _surface_crash(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("health scheduler task exited unexpectedly: %s", exc)

    task.add_done_callback(_surface_crash)
    app.state.health_scheduler_task = task


async def stop_health_scheduler(app) -> None:
    """Cancel the scheduler task on app shutdown."""
    task = getattr(app.state, "health_scheduler_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
