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
window expires and the row becomes due again.

This module never raises out of the loop; a poll that crashes is
logged and the row resweeps on the next tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.device_health_status import DeviceHealthStatus
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


async def _due_rows(db: AsyncSession, now: datetime, limit: int = 10) -> list[DeviceHealthStatus]:
    """SELECT ... FOR UPDATE SKIP LOCKED for due polling rows.

    Same pattern as inventory.apply_scheduler._due_jobs. SQLite ignores
    FOR UPDATE; the conditional UPDATE in _claim_row is the actual
    race-safety guard.
    """
    rows = await db.execute(
        select(DeviceHealthStatus)
        .where(DeviceHealthStatus.next_poll_at <= now)
        .order_by(DeviceHealthStatus.next_poll_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars().all())


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


async def _publish_health_event(nc, payload: dict) -> None:
    """Publish a health-transition event to NATS.

    Closed-default on any failure: a missed publish is tolerable since
    the snapshot in `device_health_status` still reflects the bad state
    and the next bad-news transition (if it happens) will fire again.
    No retry, no local outbox; if NATS is down the event is lost.
    """
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await js.publish(HEALTH_NATS_SUBJECT, json.dumps(payload, default=str).encode())
    except Exception:
        logger.error(
            "Failed to publish health transition event",
            extra={"action": "health_publish_failed", "payload": payload},
            exc_info=True,
        )


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
    count crosses the bad-news or recovery threshold, publish a NATS
    event to `herd.health.status_changed` after the commit.
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
    # transition check (iter 2) can fire only on threshold crossings.
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

    # ROADMAP #13 iter 2: emit a NATS event on bad-news / recovery
    # transitions. The threshold for bad_news is the same value that
    # controls the existing failure backoff, so the publish naturally
    # aligns with the scheduler's existing "device is in trouble"
    # signal. Recovery fires on the first poll that resets failures
    # to zero.
    if not settings.health_poll_notify_enabled:
        return
    transition = _decide_transition(
        old_failures=old_failures,
        new_failures=new_failures,
        threshold=settings.health_poll_max_consecutive_failures,
    )
    if transition is None:
        return
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
    await _publish_health_event(nc, payload)


# --- Loop ---


async def run_health_scheduler_loop(
    session_factory: async_sessionmaker[AsyncSession],
    nc=None,
) -> None:
    """Long-running poll loop. Cancellable via task.cancel().

    Each tick refreshes the registry if due, then picks up to 10 due
    rows and fires each one. Tick-level failures (e.g. DB unreachable)
    back off exponentially up to a cap; a healthy tick resets to the
    base interval.

    `nc` is the NATS client used to publish health-transition events
    (iter 2). Pass None to disable publishing without changing the
    `health_poll_notify_enabled` config flag.
    """
    base_interval = settings.health_poll_scheduler_tick_seconds
    max_backoff = max(base_interval * 10, 300)
    current_backoff = base_interval
    logger.info(
        "health scheduler started; tick=%ss refresh=%ss",
        base_interval,
        settings.health_poll_registry_refresh_seconds,
    )
    while True:
        tick_failed = False
        try:
            now = datetime.now(timezone.utc)
            async with httpx.AsyncClient() as client:
                async with session_factory() as db:
                    await _refresh_registry_if_due(client, now, db)
                async with session_factory() as db:
                    due = await _due_rows(db, now)
                    for row in due:
                        device_id = row.device_id
                        interval = _registry.get(device_id)
                        if interval is None:
                            # Device dropped from registry; don't poll it.
                            # Push next_poll_at far enough out that the
                            # row doesn't reappear every tick.
                            row.next_poll_at = now + timedelta(
                                seconds=settings.health_poll_registry_refresh_seconds
                            )
                            await db.commit()
                            continue
                        if not await _claim_row(db, device_id, now):
                            continue
                        try:
                            await fire_poll(session_factory, device_id, interval, nc=nc)
                        except Exception:
                            logger.exception("fire_poll crashed for device %s", device_id)
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
