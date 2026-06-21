import csv
import io
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from herd_common.enums import TopologyType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.reservation import (
    DayBucket,
    DeviceBucket,
    GroupBucket,
    TopologyTypeBucket,
    UserBucket,
    UtilizationReport,
)

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    # SQLite (test backend) drops tzinfo; Postgres preserves it. Treat naive as UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _split_hours_per_day(start: datetime, end: datetime) -> list[tuple[str, float]]:
    """Break [start, end) across UTC midnight boundaries.

    Returns a list of (YYYY-MM-DD, hours) tuples. The caller sums across these.
    """
    from datetime import timedelta as _td

    if end <= start:
        return []
    out: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        # Next midnight UTC strictly after cursor.
        day_end = datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc) + _td(days=1)
        slice_end = min(day_end, end)
        hours = (slice_end - cursor).total_seconds() / 3600.0
        if hours > 0:
            out.append((cursor.strftime("%Y-%m-%d"), hours))
        cursor = slice_end
    return out


async def build_utilization_report(
    db: AsyncSession,
    window_start: datetime,
    window_end: datetime,
    status_filter: list[ReservationStatus] | None,
) -> UtilizationReport:
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    stmt = select(Reservation).where(
        Reservation.end_time > window_start,
        Reservation.start_time < window_end,
    )
    if status_filter:
        stmt = stmt.where(Reservation.status.in_(status_filter))

    result = await db.execute(stmt)
    reservations = result.scalars().all()

    # Aggregate in Python rather than SQL. The per-device hours metric is the
    # tz-clamped overlap of the reservation with the window, computed once here
    # and reused across the user/topology/day/device buckets in a single pass;
    # a separate GROUP BY over reservation_devices would duplicate that
    # arithmetic in SQL for no real gain at current volumes. Device rows are
    # eager-loaded (selectin) with each reservation, so r.device_ids is cheap.
    user_hours: dict[uuid.UUID, float] = defaultdict(float)
    user_count: dict[uuid.UUID, int] = defaultdict(int)
    user_names: dict[uuid.UUID, str] = {}
    device_hours: dict[uuid.UUID, float] = defaultdict(float)
    device_count: dict[uuid.UUID, int] = defaultdict(int)
    topology_hours: dict[TopologyType, float] = defaultdict(float)
    topology_count: dict[TopologyType, int] = defaultdict(int)
    day_hours: dict[str, float] = defaultdict(float)
    day_count: dict[str, int] = defaultdict(int)
    total_hours = 0.0

    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)

    for r in reservations:
        effective_start = max(_as_utc(r.start_time), window_start)
        effective_end = min(_as_utc(r.end_time), window_end)
        hours = (effective_end - effective_start).total_seconds() / 3600.0
        if hours <= 0:
            continue

        user_hours[r.user_id] += hours
        user_count[r.user_id] += 1
        user_names.setdefault(r.user_id, r.owner_name or "")
        topology_hours[r.topology_type] += hours
        topology_count[r.topology_type] += 1
        total_hours += hours

        seen_days: set[str] = set()
        for day, day_slice_hours in _split_hours_per_day(effective_start, effective_end):
            day_hours[day] += day_slice_hours
            if day not in seen_days:
                day_count[day] += 1
                seen_days.add(day)

        for raw_id in r.device_ids or []:
            try:
                device_id = uuid.UUID(str(raw_id))
            except (ValueError, TypeError):
                continue
            device_hours[device_id] += hours
            device_count[device_id] += 1

    by_user = sorted(
        (
            UserBucket(
                user_id=uid,
                owner_name=user_names.get(uid, ""),
                reservation_count=user_count[uid],
                hours=round(user_hours[uid], 4),
            )
            for uid in user_hours
        ),
        key=lambda b: b.hours,
        reverse=True,
    )
    by_device = sorted(
        (
            DeviceBucket(
                device_id=did,
                reservation_count=device_count[did],
                hours=round(device_hours[did], 4),
            )
            for did in device_hours
        ),
        key=lambda b: b.hours,
        reverse=True,
    )

    by_topology_type = sorted(
        (
            TopologyTypeBucket(
                topology_type=tt,
                reservation_count=topology_count[tt],
                hours=round(topology_hours[tt], 4),
            )
            for tt in topology_hours
        ),
        key=lambda b: b.hours,
        reverse=True,
    )

    by_day = [
        DayBucket(day=d, reservation_count=day_count[d], hours=round(day_hours[d], 4))
        for d in sorted(day_hours)
    ]

    return UtilizationReport(
        window_start=window_start,
        window_end=window_end,
        total_hours=round(total_hours, 4),
        total_reservations=sum(user_count.values()),
        by_user=by_user,
        by_device=by_device,
        by_topology_type=by_topology_type,
        by_day=by_day,
    )


async def fetch_user_groups_map(
    user_ids: list[uuid.UUID],
    auth_header: str | None,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    """Look up group memberships for each user via the auth service.

    Returns a dict mapping user_id to a list of (group_id, group_name) tuples.
    Missing or unreachable users resolve to an empty list so the caller can
    bucket them under "Ungrouped" rather than fail the whole report.
    """
    empty = {uid: [] for uid in user_ids}
    if not auth_header or not user_ids:
        return empty
    out: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.auth_service_url}/groups/users/groups",
                json={"user_ids": [str(uid) for uid in user_ids]},
                headers={"Authorization": auth_header},
            )
        if resp.status_code != 200:
            logger.warning("auth /groups/users/groups returned %s", resp.status_code)
            return empty
        data = resp.json() or {}
        for uid_str, groups in data.items():
            out[uuid.UUID(uid_str)] = [
                (uuid.UUID(g["id"]), g.get("name") or "") for g in (groups or [])
            ]
    except httpx.HTTPError as exc:
        logger.warning("auth service unreachable while fetching user groups: %s", exc)
        return empty
    except Exception as exc:
        logger.warning("unexpected error in fetch_user_groups_map: %s", exc)
        return empty
    return {uid: out.get(uid, []) for uid in user_ids}


def rollup_by_group(
    by_user: list[UserBucket],
    user_groups: dict[uuid.UUID, list[tuple[uuid.UUID, str]]],
) -> list[GroupBucket]:
    hours: dict[uuid.UUID | None, float] = defaultdict(float)
    counts: dict[uuid.UUID | None, int] = defaultdict(int)
    names: dict[uuid.UUID | None, str] = {None: "Ungrouped"}
    for bucket in by_user:
        groups = user_groups.get(bucket.user_id) or []
        if not groups:
            hours[None] += bucket.hours
            counts[None] += bucket.reservation_count
            continue
        # A user can be in multiple groups; double-counting is intentional
        # so each group's line reflects its members' full usage.
        for gid, gname in groups:
            hours[gid] += bucket.hours
            counts[gid] += bucket.reservation_count
            names.setdefault(gid, gname)
    buckets = [
        GroupBucket(
            group_id=gid,
            group_name=names.get(gid) or "",
            reservation_count=counts[gid],
            hours=round(hours[gid], 4),
        )
        for gid in hours
    ]
    return sorted(buckets, key=lambda b: b.hours, reverse=True)


async def fetch_execution_run_count(
    window_start: datetime,
    window_end: datetime,
    auth_header: str | None,
) -> int | None:
    """Query the execution service for the number of runs started in the window.

    Returns None if the execution service is unreachable or responds with an error.
    The report remains useful either way; the caller decides how to render a None.
    """
    if not auth_header:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.execution_service_url}/runs",
                params={
                    "created_after": window_start.isoformat(),
                    "created_before": window_end.isoformat(),
                    "limit": 1,
                },
                headers={"Authorization": auth_header},
            )
    except httpx.HTTPError as exc:
        logger.warning("execution service unreachable for run-count lookup: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("execution run-count returned %s: %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    total = data.get("total")
    return int(total) if isinstance(total, int) else None


def report_to_csv(report: UtilizationReport, section: str) -> str:
    """Render one section of a UtilizationReport as CSV text.

    Supported sections: "user", "device". "template" is a client-side rollup
    because it requires joining against the inventory service.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    if section == "user":
        writer.writerow(["user_id", "owner_name", "hours", "reservation_count"])
        for b in report.by_user:
            writer.writerow([str(b.user_id), b.owner_name, f"{b.hours:.4f}", b.reservation_count])
    elif section == "device":
        writer.writerow(["device_id", "hours", "reservation_count"])
        for b in report.by_device:
            writer.writerow([str(b.device_id), f"{b.hours:.4f}", b.reservation_count])
    else:
        raise ValueError(f"Unsupported CSV section: {section}")
    return buf.getvalue()
