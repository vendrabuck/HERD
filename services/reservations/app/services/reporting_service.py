import csv
import io
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from herd_common.enums import TopologyType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.reservation import (
    DayBucket,
    DeviceBucket,
    DevicePurposeBucket,
    FleetDeviceBucket,
    FleetSection,
    GroupBucket,
    PurposeBucket,
    TopologyTypeBucket,
    UserBucket,
    UserPurposeBucket,
    UtilizationReport,
)

logger = logging.getLogger(__name__)

# Literal bucket label for a reservation with no purpose_category (issue #646
# phase 1). Never a null value in the report, so a client can group on the
# field without a null-handling special case.
UNCLASSIFIED_PURPOSE = "unclassified"


def _as_utc(dt: datetime) -> datetime:
    # SQLite (test backend) drops tzinfo; Postgres preserves it. Treat naive as UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _split_hours_per_day(start: datetime, end: datetime) -> list[tuple[str, float]]:
    """Break [start, end) across UTC midnight boundaries.

    Returns a list of (YYYY-MM-DD, hours) tuples. The caller sums across these.
    """
    if end <= start:
        return []
    out: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        # Next midnight UTC strictly after cursor.
        day_end = datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc) + timedelta(
            days=1
        )
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
    fleet_status_filter: list[ReservationStatus] | None = None,
    fleet_devices: list[dict] | None = None,
) -> UtilizationReport:
    """Aggregate reservation hours over a window.

    The legacy sections (by_user/by_device/by_topology_type/by_day) honor
    status_filter. The fleet section honors fleet_status_filter, which may
    differ: the route defaults it to ACTIVE+COMPLETED so in-flight usage
    counts toward utilization, while the legacy default stays COMPLETED.
    Both are gathered in one query over the union and bucketed per section
    in the same pass. fleet_devices is inventory's device list (id/name/
    status dicts); None means inventory was unreachable or was not asked,
    and the fleet section is omitted (report.fleet stays None).

    Raises ValueError (422 at the router) when the requested window's span
    exceeds utilization_max_span_days (issue #389): the query below has no
    LIMIT and no pagination, so an unbounded client-controlled window would
    otherwise load and hold an unbounded result set in memory. 0 disables
    the cap.
    """
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    max_span_days = settings.utilization_max_span_days
    if max_span_days > 0 and (window_end - window_start) > timedelta(days=max_span_days):
        raise ValueError(
            f"Utilization window cannot exceed {max_span_days} days "
            f"(requested {window_start.isoformat()} to {window_end.isoformat()})"
        )

    if fleet_status_filter is None:
        fleet_status_filter = status_filter
    legacy_set = set(status_filter) if status_filter else None
    fleet_set = set(fleet_status_filter) if fleet_status_filter else None
    if legacy_set is None or (fleet_devices is not None and fleet_set is None):
        query_statuses = None  # at least one section wants every status
    elif fleet_devices is not None:
        query_statuses = legacy_set | fleet_set
    else:
        query_statuses = legacy_set

    stmt = select(Reservation).where(
        Reservation.end_time > window_start,
        Reservation.start_time < window_end,
    )
    if query_statuses:
        stmt = stmt.where(Reservation.status.in_(query_statuses))

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
    fleet_device_hours: dict[uuid.UUID, float] = defaultdict(float)
    fleet_device_count: dict[uuid.UUID, int] = defaultdict(int)
    total_hours = 0.0

    # Lab purpose classification breakdowns (issue #646 phase 1). device_hours
    # here means actual device-hours (a reservation's hours times how many
    # devices it reserved), not the per-reservation hours the other legacy
    # buckets track, matching the report's `device_hours` field name.
    # Reserved devices only: there is no transit-gear tracking to exclude yet
    # (phase 3), so every id in r.device_ids already qualifies.
    purpose_hours: dict[str, float] = defaultdict(float)
    purpose_count: dict[str, int] = defaultdict(int)
    user_purpose_hours: dict[tuple[uuid.UUID, str], float] = defaultdict(float)
    user_purpose_count: dict[tuple[uuid.UUID, str], int] = defaultdict(int)
    device_purpose_hours: dict[tuple[uuid.UUID, str], float] = defaultdict(float)
    device_purpose_count: dict[tuple[uuid.UUID, str], int] = defaultdict(int)

    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)

    for r in reservations:
        effective_start = max(_as_utc(r.start_time), window_start)
        effective_end = min(_as_utc(r.end_time), window_end)
        hours = (effective_end - effective_start).total_seconds() / 3600.0
        if hours <= 0:
            continue

        in_legacy = legacy_set is None or r.status in legacy_set
        in_fleet = fleet_devices is not None and (fleet_set is None or r.status in fleet_set)

        if in_legacy:
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

            category = r.purpose_category or UNCLASSIFIED_PURPOSE
            purpose_count[category] += 1
            user_purpose_count[(r.user_id, category)] += 1

        if not in_legacy and not in_fleet:
            continue
        for raw_id in r.device_ids or []:
            try:
                device_id = uuid.UUID(str(raw_id))
            except (ValueError, TypeError):
                continue
            if in_legacy:
                device_hours[device_id] += hours
                device_count[device_id] += 1
                purpose_hours[category] += hours
                user_purpose_hours[(r.user_id, category)] += hours
                device_purpose_hours[(device_id, category)] += hours
                device_purpose_count[(device_id, category)] += 1
            if in_fleet:
                fleet_device_hours[device_id] += hours
                fleet_device_count[device_id] += 1

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

    by_purpose = sorted(
        (
            PurposeBucket(
                purpose_category=category,
                reservations=purpose_count[category],
                device_hours=round(purpose_hours[category], 4),
            )
            for category in purpose_count
        ),
        key=lambda b: b.device_hours,
        reverse=True,
    )
    by_user_purpose = sorted(
        (
            UserPurposeBucket(
                user_id=uid,
                purpose_category=category,
                reservations=user_purpose_count[(uid, category)],
                device_hours=round(user_purpose_hours[(uid, category)], 4),
            )
            for uid, category in user_purpose_count
        ),
        key=lambda b: b.device_hours,
        reverse=True,
    )
    by_device_purpose = sorted(
        (
            DevicePurposeBucket(
                device_id=did,
                purpose_category=category,
                reservations=device_purpose_count[(did, category)],
                device_hours=round(device_purpose_hours[(did, category)], 4),
            )
            for did, category in device_purpose_count
        ),
        key=lambda b: b.device_hours,
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

    fleet = None
    if fleet_devices is not None:
        fleet = _build_fleet_section(
            fleet_devices, fleet_device_hours, fleet_device_count, window_start, window_end
        )

    return UtilizationReport(
        window_start=window_start,
        window_end=window_end,
        total_hours=round(total_hours, 4),
        total_reservations=sum(user_count.values()),
        by_user=by_user,
        by_device=by_device,
        by_topology_type=by_topology_type,
        by_day=by_day,
        fleet=fleet,
        by_purpose=by_purpose,
        by_user_purpose=by_user_purpose,
        by_device_purpose=by_device_purpose,
    )


def _build_fleet_section(
    fleet_devices: list[dict],
    fleet_device_hours: dict[uuid.UUID, float],
    fleet_device_count: dict[uuid.UUID, int],
    window_start: datetime,
    window_end: datetime,
) -> FleetSection:
    """Join reserved hours against the full inventory device list.

    Every inventory device appears, including ones with zero bookings (those
    ARE the idle list). The denominator is the full window for every device:
    device status has no history table, so a downtime-adjusted denominator
    would be invented data; current status ships as a column instead.
    utilization_pct is deliberately not clamped: conflict detection caps it
    at 100 only when CANCELLED/FAILED are excluded, and a value over 100
    under a wider filter is information.

    Devices deleted from inventory but present in reservation history are
    omitted here; they still appear in the legacy by_device section.
    """
    window_hours = (window_end - window_start).total_seconds() / 3600.0
    buckets: list[FleetDeviceBucket] = []
    total_reserved = 0.0
    idle = 0
    for d in fleet_devices:
        try:
            device_id = uuid.UUID(str(d["id"]))
        except (KeyError, ValueError, TypeError):
            logger.warning("fleet report skipping inventory device with unusable id: %r", d)
            continue
        hours = fleet_device_hours.get(device_id, 0.0)
        total_reserved += hours
        if hours <= 0:
            idle += 1
        buckets.append(
            FleetDeviceBucket(
                device_id=device_id,
                name=str(d.get("name") or ""),
                status=str(d.get("status") or ""),
                reservation_count=fleet_device_count.get(device_id, 0),
                hours=round(hours, 4),
                utilization_pct=round(hours / window_hours * 100.0, 2) if window_hours else 0.0,
            )
        )
    buckets.sort(key=lambda b: (-b.utilization_pct, b.name))
    denominator = len(buckets) * window_hours
    return FleetSection(
        device_count=len(buckets),
        idle_device_count=idle,
        window_hours=round(window_hours, 4),
        total_reserved_hours=round(total_reserved, 4),
        utilization_pct=round(total_reserved / denominator * 100.0, 2) if denominator else 0.0,
        devices=buckets,
    )


async def fetch_fleet_devices(auth_header: str | None) -> list[dict] | None:
    """Fetch the full device list from inventory for the fleet section.

    Paginates GET /devices with the forwarded admin JWT (an admin caller sees
    every device). Returns None on any failure so the report degrades to
    fleet=null instead of failing outright, matching fetch_user_groups_map
    and fetch_execution_run_count.
    """
    if not auth_header:
        return None
    devices: list[dict] = []
    skip = 0
    limit = 500
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                resp = await client.get(
                    f"{settings.inventory_service_url}/devices",
                    params={"skip": skip, "limit": limit},
                    headers={"Authorization": auth_header},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "inventory /devices returned %s while building the fleet report",
                        resp.status_code,
                    )
                    return None
                data = resp.json() or {}
                items = data.get("items") or []
                devices.extend(items)
                total = data.get("total")
                skip += limit
                if len(items) < limit or (isinstance(total, int) and len(devices) >= total):
                    break
                if skip >= 100_000:
                    # Safety valve: a fleet this large means something is wrong
                    # (or reporting needs a server-side aggregation rethink).
                    logger.warning(
                        "fleet report device fetch aborted after %s devices", len(devices)
                    )
                    return None
    except httpx.HTTPError as exc:
        logger.warning("inventory service unreachable while building the fleet report: %s", exc)
        return None
    return devices


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

    Supported sections: "user", "device", "fleet", "purpose", "user_purpose",
    "device_purpose". "template" is a client-side rollup because it requires
    joining against the inventory service. "fleet" requires report.fleet to be
    populated; the route turns an unpopulated fleet into a 503 before calling
    this. The three purpose sections (issue #646 phase 1) are new, standalone
    sections rather than an added column on "user"/"device": a user or device
    can span multiple purpose categories, so a per-user or per-device row
    cannot carry a single category value without losing information.
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
    elif section == "fleet":
        if report.fleet is None:
            raise ValueError("fleet section is not populated on this report")
        writer.writerow(
            ["device_id", "name", "status", "hours", "utilization_pct", "reservation_count"]
        )
        for b in report.fleet.devices:
            writer.writerow(
                [
                    str(b.device_id),
                    b.name,
                    b.status,
                    f"{b.hours:.4f}",
                    f"{b.utilization_pct:.2f}",
                    b.reservation_count,
                ]
            )
    elif section == "purpose":
        writer.writerow(["purpose_category", "reservations", "device_hours"])
        for b in report.by_purpose:
            writer.writerow([b.purpose_category, b.reservations, f"{b.device_hours:.4f}"])
    elif section == "user_purpose":
        writer.writerow(["user_id", "purpose_category", "reservations", "device_hours"])
        for b in report.by_user_purpose:
            writer.writerow(
                [str(b.user_id), b.purpose_category, b.reservations, f"{b.device_hours:.4f}"]
            )
    elif section == "device_purpose":
        writer.writerow(["device_id", "purpose_category", "reservations", "device_hours"])
        for b in report.by_device_purpose:
            writer.writerow(
                [str(b.device_id), b.purpose_category, b.reservations, f"{b.device_hours:.4f}"]
            )
    else:
        raise ValueError(f"Unsupported CSV section: {section}")
    return buf.getvalue()
