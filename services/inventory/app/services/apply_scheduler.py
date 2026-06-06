"""Background loop that fires due device-config apply jobs.

The scheduler polls `device_config_apply_jobs` every
`settings.apply_scheduler_interval_seconds` for rows where
`status='pending' AND scheduled_for <= now()`. Each due job is claimed by
flipping its status to `running`, then resolved in one of three ways:

- `success` if the execution service returns 2xx with a SUCCESS run.
- `skipped` if the job had a `reservation_id` and the reservations service
  reports the reservation is not currently active.
- `failed` for any other outcome (HTTP error, non-2xx, malformed payload).

The loop authenticates to execution via INTERNAL_API_TOKEN at
`/execute/internal`; the reservations check uses the same token where needed.
The pipeline never raises out of the loop, so a single bad job cannot stall
the others.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion

logger = logging.getLogger(__name__)


async def _due_jobs(db: AsyncSession, now: datetime, limit: int = 10) -> list[DeviceConfigApplyJob]:
    """Fetch due pending jobs, locking them against concurrent schedulers.

    Uses SELECT ... FOR UPDATE SKIP LOCKED on Postgres so two inventory replicas
    cannot pull the same row in the same tick. SQLite (test backend) silently
    ignores FOR UPDATE; tests cover the lost-race path via the conditional
    UPDATE in `fire_job` instead.

    Locks release on the next commit on this session. The per-job loop calls
    `fire_job`, which commits at each terminal flip, so locks on remaining rows
    in this batch are released between jobs. The conditional UPDATE in
    `fire_job`'s claim step closes the resulting window.
    """
    rows = await db.execute(
        select(DeviceConfigApplyJob)
        .where(
            DeviceConfigApplyJob.status == "pending",
            DeviceConfigApplyJob.scheduled_for <= now,
        )
        .order_by(DeviceConfigApplyJob.scheduled_for.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars().all())


async def _reservation_active(client: httpx.AsyncClient, reservation_id: uuid.UUID) -> bool:
    """Check whether a reservation is currently active.

    Calls the reservations service `/internal/{id}` endpoint with X-Internal-Token.
    The server returns a precomputed `is_active` boolean that combines status with
    the time window, so this caller does not duplicate that logic. Returns False
    on any failure (closed-default), since the reservation gate is a defense-in-depth
    control and unreachable means "do not fire".
    """
    if not settings.internal_api_token:
        return False
    url = f"{settings.reservations_service_url.rstrip('/')}/internal/{reservation_id}"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        resp = await client.get(url, headers=headers, timeout=5.0)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return bool(data.get("is_active"))


async def _post_internal_execute(
    client: httpx.AsyncClient,
    job: DeviceConfigApplyJob,
    config: dict,
) -> tuple[str, uuid.UUID | None, str | None]:
    """POST to /execute/internal. Returns (status, run_id, error)."""
    url = f"{settings.execution_service_url.rstrip('/')}/execute/internal"
    headers = {"X-Internal-Token": settings.internal_api_token}
    body = {
        "device_id": str(job.device_id),
        "action": "configure",
        "user_id": str(job.created_by),
        "method_kwargs": config,
        "dry_run": bool(job.dry_run),
    }
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        return "failed", None, f"execution unreachable: {exc}"
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        return "failed", None, f"{resp.status_code} {detail}"
    try:
        data = resp.json()
    except ValueError:
        return "failed", None, "execution returned malformed JSON"
    run_id_str = data.get("id")
    run_id = None
    if run_id_str:
        try:
            run_id = uuid.UUID(run_id_str)
        except (ValueError, TypeError):
            run_id = None
    run_status = str(data.get("status", "SUCCESS")).upper()
    if run_status == "SUCCESS":
        return "success", run_id, None
    return "failed", run_id, str(data.get("error") or "execution returned non-success status")


STALE_RUNNING_AFTER_SECONDS = 300


async def _resweep_stale_running(
    db: AsyncSession,
    now: datetime,
    stale_after_seconds: int = STALE_RUNNING_AFTER_SECONDS,
) -> int:
    """Re-queue jobs left in `running` past the stale threshold.

    A stuck-running job has status='running', fired_at=NULL (terminal flip never
    happened), and scheduled_for older than the threshold. The WHERE clause is
    selective enough that we will not race a job that is legitimately still
    being worked: a healthy claim flips status='running' within ms of
    scheduled_for, so anything more than five minutes past schedule with
    fired_at still NULL has crashed.

    Returns the row count that was re-queued (useful for tests + logs).
    """
    threshold = now - timedelta(seconds=stale_after_seconds)
    result = await db.execute(
        update(DeviceConfigApplyJob)
        .where(
            DeviceConfigApplyJob.status == "running",
            DeviceConfigApplyJob.fired_at.is_(None),
            DeviceConfigApplyJob.scheduled_for < threshold,
        )
        .values(status="pending")
    )
    await db.commit()
    if result.rowcount:
        logger.warning("resweep: re-queued %d stale-running apply job(s)", result.rowcount)
    return result.rowcount or 0


async def _mark_failed_in_fresh_session(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    error: str,
) -> None:
    """Record a job failure using a fresh session.

    Used after fire_job raises so we do not depend on the session that just
    crashed. Swallows secondary errors: this is best-effort cleanup, the
    resweep will catch anything we still miss.
    """
    try:
        async with session_factory() as fresh:
            await fresh.execute(
                update(DeviceConfigApplyJob)
                .where(DeviceConfigApplyJob.id == job_id)
                .values(
                    status="failed",
                    error=error,
                    fired_at=datetime.now(timezone.utc),
                )
            )
            await fresh.commit()
    except Exception:
        logger.exception("could not record fire_job crash for job %s", job_id)


async def fire_job(
    db: AsyncSession,
    job: DeviceConfigApplyJob,
    client: httpx.AsyncClient,
) -> None:
    """Drive a single due job through to terminal state.

    The claim step is a conditional UPDATE: only flip to `running` if the row
    is still `pending`. If another replica raced us and won the lock, the
    UPDATE affects 0 rows and we return without firing. This is the canonical
    optimistic-concurrency claim, paired with the FOR UPDATE SKIP LOCKED in
    `_due_jobs` it makes the claim race-safe.
    """
    claim = await db.execute(
        update(DeviceConfigApplyJob)
        .where(
            DeviceConfigApplyJob.id == job.id,
            DeviceConfigApplyJob.status == "pending",
        )
        .values(status="running")
    )
    await db.commit()
    if claim.rowcount == 0:
        logger.info("apply job %s already claimed by another scheduler; skipping", job.id)
        return
    await db.refresh(job)

    if job.reservation_id is not None:
        active = await _reservation_active(client, job.reservation_id)
        if not active:
            job.status = "skipped"
            job.error = "reservation not currently active"
            job.fired_at = datetime.now(timezone.utc)
            await db.commit()
            return

    version = await db.get(DeviceConfigVersion, job.version_id)
    if version is None:
        job.status = "failed"
        job.error = "config version no longer exists"
        job.fired_at = datetime.now(timezone.utc)
        await db.commit()
        return

    new_status, run_id, error = await _post_internal_execute(client, job, version.config)
    job.status = new_status
    job.run_id = run_id
    job.error = error
    job.fired_at = datetime.now(timezone.utc)
    if new_status == "success" and run_id is not None:
        version.last_apply_run_id = run_id
    await db.commit()


async def run_scheduler_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Long-running poll loop. Cancellable via task.cancel().

    Each tick: re-queue stale-running rows, then fetch + fire due jobs. A tick
    that fails (e.g. DB unreachable) backs off exponentially up to a cap so a
    long outage does not keep hammering the upstream at the base interval. A
    successful tick resets the backoff to the base interval.
    """
    interval = settings.apply_scheduler_interval_seconds
    max_backoff = max(interval * 10, 300)
    current_backoff = interval
    logger.info("apply scheduler started; interval=%ss", interval)
    while True:
        tick_failed = False
        try:
            now = datetime.now(timezone.utc)
            async with session_factory() as db:
                await _resweep_stale_running(db, now)
                due = await _due_jobs(db, now)
                if due:
                    async with httpx.AsyncClient() as client:
                        for job in due:
                            try:
                                await fire_job(db, job, client)
                            except Exception:
                                logger.exception("fire_job crashed for job %s", job.id)
                                await _mark_failed_in_fresh_session(
                                    session_factory, job.id, "scheduler crash"
                                )
        except asyncio.CancelledError:
            logger.info("apply scheduler cancelled; exiting loop")
            raise
        except Exception:
            logger.exception("apply scheduler tick failed")
            tick_failed = True

        current_backoff = min(current_backoff * 2, max_backoff) if tick_failed else interval

        try:
            await asyncio.sleep(current_backoff)
        except asyncio.CancelledError:
            raise
