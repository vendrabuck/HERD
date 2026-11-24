"""The utilization report's day section is split at UTC midnights for any offset (issue #1523).

OPS-REPORT-4. Before the fix, ``_split_hours_per_day`` built "the next UTC midnight"
from the LOCAL date fields of an offset-aware bound and labeled each slice with the
local date, and the report routes passed ``start`` and ``end`` through as parsed. A
``start`` late in its local day with a negative offset (2026-03-31T20:00-08:00, which
is 2026-04-01T04:00Z) put that midnight before the start, so the hours between UTC
midnight and the start were counted: ``total_hours`` 8.0, ``by_day`` 12.0. A positive
offset kept the sum but put hours under the wrong UTC date.

The invariant pinned here, for any pair of bounds a caller can send (UTC, a positive
or negative offset, mixed offsets on start and end, a bound without an offset, which
FastAPI parses as a naive datetime and the report takes as UTC):

- the helper's slices are positive, contiguous, split exactly at UTC midnights,
  labeled with the UTC date, and sum to ``end - start``;
- the report's ``by_day`` hours sum to its ``total_hours``;
- the routes hand UTC bounds to everything downstream (the run-count query, the CSV
  file name), and a naive bound means UTC whatever the host's local zone is.

Unit cases are hand-computed; the property check compares the helper against an
independent date-indexed oracle over a seeded random sample (no new dependency) and
asserts the sample really covers each shape it claims to.
"""

import math
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.main import app
from app.models.reservation import Reservation, ReservationStatus
from app.routers.reservations import bearer_scheme
from app.services.reporting_service import _split_hours_per_day, build_utilization_report
from herd_common.enums import TopologyType
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests._harness import TestSessionLocal, override_bearer, override_get_db

UTC = timezone.utc
USER = uuid.uuid4()


def _tz(hours: int, minutes: int = 0) -> timezone:
    sign = -1 if hours < 0 else 1
    return timezone(timedelta(hours=hours, minutes=sign * minutes))


# --- unit: the helper, hand-computed cases ---


def test_split_hours_negative_offset_start_late_in_local_day():
    """The issue's reproduction: 20:00-08:00 is 04:00Z, so 2026-04-01 has 8 hours, not 12."""
    start = datetime(2026, 3, 31, 20, 0, tzinfo=_tz(-8))
    end = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    assert _split_hours_per_day(start, end) == [("2026-04-01", 8.0)]


def test_split_hours_positive_offset_labels_the_utc_dates():
    """02:00+05:00 is 21:00Z the day before: three hours belong to 2026-03-31."""
    start = datetime(2026, 4, 1, 2, 0, tzinfo=_tz(5))
    end = datetime(2026, 4, 1, 3, 0, tzinfo=UTC)
    assert _split_hours_per_day(start, end) == [("2026-03-31", 3.0), ("2026-04-01", 3.0)]


def test_split_hours_local_midnight_crossed_but_no_utc_midnight():
    """Both bounds are on 2026-04-02 locally (+05:00) and on 2026-04-01 in UTC."""
    start = datetime(2026, 4, 2, 1, 0, tzinfo=_tz(5))
    end = datetime(2026, 4, 2, 3, 0, tzinfo=_tz(5))
    assert _split_hours_per_day(start, end) == [("2026-04-01", 2.0)]


def test_split_hours_negative_offset_crossing_one_utc_midnight():
    start = datetime(2026, 4, 1, 15, 0, tzinfo=_tz(-8))  # 23:00Z
    end = datetime(2026, 4, 1, 18, 0, tzinfo=_tz(-8))  # 02:00Z on 2026-04-02
    assert _split_hours_per_day(start, end) == [("2026-04-01", 1.0), ("2026-04-02", 2.0)]


def test_split_hours_mixed_offsets_crossing_several_utc_midnights():
    """+05:30 start, -08:00 end, across the end of February (2026 is not a leap year)."""
    start = datetime(2026, 2, 27, 22, 30, tzinfo=_tz(5, 30))  # 2026-02-27T17:00Z
    end = datetime(2026, 3, 1, 20, 0, tzinfo=_tz(-8))  # 2026-03-02T04:00Z
    assert _split_hours_per_day(start, end) == [
        ("2026-02-27", 7.0),
        ("2026-02-28", 24.0),
        ("2026-03-01", 24.0),
        ("2026-03-02", 4.0),
    ]


def test_split_hours_start_exactly_at_utc_midnight_in_an_offset():
    """16:00-08:00 IS UTC midnight: no slice, empty or otherwise, for 2026-03-31."""
    start = datetime(2026, 3, 31, 16, 0, tzinfo=_tz(-8))
    end = datetime(2026, 4, 2, 6, 0, tzinfo=UTC)
    assert _split_hours_per_day(start, end) == [("2026-04-01", 24.0), ("2026-04-02", 6.0)]


def test_split_hours_end_exactly_at_utc_midnight_in_an_offset():
    """19:00-05:00 is the next UTC midnight: no zero-hour slice for 2026-04-02."""
    start = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 1, 19, 0, tzinfo=_tz(-5))
    assert _split_hours_per_day(start, end) == [("2026-04-01", 14.0)]


def test_split_hours_naive_bounds_are_utc():
    start = datetime(2026, 4, 1, 22, 0)
    end = datetime(2026, 4, 2, 1, 0)
    assert _split_hours_per_day(start, end) == [("2026-04-01", 2.0), ("2026-04-02", 1.0)]


def test_split_hours_naive_bounds_are_utc_under_a_minus_eight_host_zone(host_zone_minus_eight):
    """The same naive bounds with the host at -08:00: astimezone() would read them
    as 06:00Z and 09:00Z on 2026-04-02; the helper must still take them as UTC."""
    assert datetime(2026, 4, 1, 22, 0).astimezone().utcoffset() == timedelta(hours=-8)
    test_split_hours_naive_bounds_are_utc()


def test_split_hours_last_representable_day_has_no_next_midnight():
    """A slice on 9999-12-31 runs to end; computing that day's next midnight overflows."""
    start = datetime(9999, 12, 30, 20, 0, tzinfo=UTC)
    end = datetime(9999, 12, 31, 13, 0, tzinfo=_tz(-5))  # 18:00Z
    assert _split_hours_per_day(start, end) == [("9999-12-30", 4.0), ("9999-12-31", 18.0)]
    last = datetime.max.replace(tzinfo=UTC)
    got = _split_hours_per_day(datetime(9999, 12, 31, tzinfo=UTC), last)
    assert [label for label, _ in got] == ["9999-12-31"]
    assert got[0][1] == pytest.approx(24.0)


def test_split_hours_labels_a_year_before_1000_with_four_digits():
    """strftime("%Y") gives "1-01-01" on Python 3.12; the label is ISO on every version."""
    start = datetime(1, 1, 1, 1, 0, tzinfo=_tz(1))  # 0001-01-01T00:00Z, the first instant
    end = datetime(1, 1, 2, 2, 0, tzinfo=UTC)
    assert _split_hours_per_day(start, end) == [("0001-01-01", 24.0), ("0001-01-02", 2.0)]


def test_split_hours_naive_start_with_an_offset_end():
    """A naive start is UTC; comparing it with an aware end must not raise."""
    start = datetime(2026, 4, 1, 22, 0)
    end = datetime(2026, 4, 1, 17, 0, tzinfo=_tz(-8))  # 2026-04-02T01:00Z
    assert _split_hours_per_day(start, end) == [("2026-04-01", 2.0), ("2026-04-02", 1.0)]


def test_split_hours_order_is_by_instant_not_by_wall_clock():
    """Wall clocks say 10:00 before 08:00, the instants say 05:00Z before 08:00Z."""
    early = datetime(2026, 4, 1, 10, 0, tzinfo=_tz(5))  # 05:00Z
    late = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    assert _split_hours_per_day(early, late) == [("2026-04-01", 3.0)]
    assert _split_hours_per_day(late, early) == []


# --- unit: the helper, property check against an independent oracle ---

# Minutes east of UTC; None renders the bound naive. The extremes (-12:00, +14:00) and
# the non-hour offsets (-03:30, +05:30, +05:45) are real zones.
_OFFSETS = (None, 0, -12 * 60, -8 * 60, -210, 330, 345, 14 * 60)


def _render(instant: datetime, offset_minutes: int | None) -> datetime:
    if offset_minutes is None:
        return instant.replace(tzinfo=None)
    return instant.astimezone(timezone(timedelta(minutes=offset_minutes)))


def _oracle(start: datetime, end: datetime) -> list[tuple[str, float]]:
    """Date-indexed, not cursor-based: the overlap of [start, end) with each UTC day."""
    out: list[tuple[str, float]] = []
    day = start.date()
    while True:
        lo = datetime(day.year, day.month, day.day, tzinfo=UTC)
        if lo >= end:
            return out
        hi = lo + timedelta(days=1)
        a, b = max(lo, start), min(hi, end)
        out.append((day.isoformat(), (b - a).total_seconds() / 3600.0))
        day += timedelta(days=1)


def _midnight(instant: datetime) -> datetime:
    return instant.replace(hour=0, minute=0, second=0, microsecond=0)


def test_split_hours_property_any_offsets_any_window():
    rng = random.Random(1523)
    base = datetime(2023, 12, 20, tzinfo=UTC)  # the sample spans two year ends and 2024-02-29
    seen = {
        "no_midnight": 0,
        "one_midnight": 0,
        "several_midnights": 0,
        "start_at_utc_midnight": 0,
        "end_at_utc_midnight": 0,
        "mixed_offsets": 0,
        "naive_bound": 0,
        "local_date_differs": 0,
    }
    for _ in range(4000):
        start_utc = base + timedelta(
            seconds=rng.randrange(0, 3 * 366 * 86400), microseconds=rng.randrange(0, 1_000_000)
        )
        if rng.random() < 0.15:
            start_utc = _midnight(start_utc)
        span = rng.choice(
            (
                timedelta(seconds=rng.randrange(1, 4 * 3600)),
                timedelta(seconds=rng.randrange(1, 2 * 86400)),
                timedelta(seconds=rng.randrange(1, 10 * 86400)),
                timedelta(days=rng.randrange(1, 5)),
            )
        )
        end_utc = start_utc + span
        if rng.random() < 0.15:
            end_utc = _midnight(end_utc) + timedelta(days=1)
        start_off, end_off = rng.choice(_OFFSETS), rng.choice(_OFFSETS)
        start, end = _render(start_utc, start_off), _render(end_utc, end_off)

        got = _split_hours_per_day(start, end)

        assert got == _oracle(start_utc, end_utc), (start.isoformat(), end.isoformat())
        assert got, (start.isoformat(), end.isoformat())
        assert all(hours > 0 for _, hours in got)
        days = [date.fromisoformat(label) for label, _ in got]
        assert days[0] == start_utc.date()
        assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:]))
        assert all(hours == 24.0 for _, hours in got[1:-1])
        assert math.isclose(
            sum(hours for _, hours in got),
            (end_utc - start_utc).total_seconds() / 3600.0,
            rel_tol=1e-12,
            abs_tol=1e-9,
        )

        crossings = len(got) - 1
        seen[
            "no_midnight"
            if crossings == 0
            else "one_midnight"
            if crossings == 1
            else "several_midnights"
        ] += 1
        seen["start_at_utc_midnight"] += start_utc == _midnight(start_utc)
        seen["end_at_utc_midnight"] += end_utc == _midnight(end_utc)
        seen["mixed_offsets"] += start_off != end_off
        seen["naive_bound"] += start_off is None or end_off is None
        seen["local_date_differs"] += start_off is not None and start.date() != start_utc.date()
    # The sample must really contain every shape the docstring claims it covers.
    assert all(count >= 100 for count in seen.values()), seen


# --- unit: the report, by_day against total_hours ---


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _reservation(start: datetime, end: datetime) -> Reservation:
    return Reservation(
        id=uuid.uuid4(),
        user_id=USER,
        owner_name="alice",
        device_ids=[str(uuid.uuid4())],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=start,
        end_time=end,
        status=ReservationStatus.COMPLETED,
    )


async def test_build_report_by_day_sums_to_total_for_any_offset_window(db_session):
    """Seeded windows in every offset over four overlapping reservations."""
    anchor = datetime(2026, 3, 30, tzinfo=UTC)
    for start_h, end_h in ((0, 30), (13, 61), (40, 41), (47.5, 96)):
        db_session.add(
            _reservation(anchor + timedelta(hours=start_h), anchor + timedelta(hours=end_h))
        )
    await db_session.commit()

    rng = random.Random(15231)
    for _ in range(60):
        start_utc = anchor + timedelta(minutes=rng.randrange(-6 * 60, 90 * 60))
        end_utc = start_utc + timedelta(minutes=rng.randrange(1, 4 * 24 * 60))
        start = _render(start_utc, rng.choice(_OFFSETS))
        end = _render(end_utc, rng.choice(_OFFSETS))
        report = await build_utilization_report(db_session, start, end, None, include_transit=False)
        day_sum = sum(b.hours for b in report.by_day)
        # Each bucket and the total are rounded to four places on their own.
        assert day_sum == pytest.approx(report.total_hours, abs=1e-4 * (len(report.by_day) + 1))
        assert all(b.hours > 0 for b in report.by_day)
        assert [b.day for b in report.by_day] == sorted(b.day for b in report.by_day)
        if report.by_day:
            assert report.by_day[0].day >= start_utc.date().isoformat()
            assert report.by_day[-1].day <= end_utc.date().isoformat()


async def test_build_report_takes_a_naive_and_an_offset_bound_and_echoes_utc(db_session):
    db_session.add(
        _reservation(datetime(2026, 3, 31, tzinfo=UTC), datetime(2026, 4, 2, tzinfo=UTC))
    )
    await db_session.commit()
    report = await build_utilization_report(
        db_session,
        datetime(2026, 3, 31, 20, 0),  # naive: UTC
        datetime(2026, 4, 1, 2, 0, tzinfo=_tz(-8)),  # 2026-04-01T10:00Z
        None,
        include_transit=False,
    )
    assert report.window_start == datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
    assert report.window_start.utcoffset() == timedelta(0)
    assert report.window_end.utcoffset() == timedelta(0)
    assert report.total_hours == 14.0
    assert [(b.day, b.hours) for b in report.by_day] == [("2026-03-31", 4.0), ("2026-04-01", 10.0)]


# --- functional: both report routes through the ASGI app ---


@pytest.fixture
async def admin_client():
    payload = {"sub": str(USER), "username": "adminuser", "role": "admin"}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    app.dependency_overrides[require_admin] = lambda: payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed(start: datetime, end: datetime) -> None:
    async with TestSessionLocal() as session:
        session.add(_reservation(start, end))
        await session.commit()


REPRO = {"start": "2026-03-31T20:00:00-08:00", "end": "2026-04-01T12:00:00Z"}


@pytest.mark.parametrize(
    ("start", "end", "by_day", "total"),
    [
        # The issue's reproduction.
        (REPRO["start"], REPRO["end"], [("2026-04-01", 8.0)], 8.0),
        # A positive offset: the first three hours are 2026-03-31 in UTC.
        (
            "2026-04-01T02:00:00+05:00",
            "2026-04-01T08:00:00+05:00",
            [("2026-03-31", 3.0), ("2026-04-01", 3.0)],
            6.0,
        ),
        # Mixed offsets across two UTC midnights (17:00Z to 04:00Z two days on).
        (
            "2026-03-31T22:30:00+05:30",
            "2026-04-01T20:00:00-08:00",
            [("2026-03-31", 7.0), ("2026-04-01", 24.0), ("2026-04-02", 4.0)],
            35.0,
        ),
        # A start exactly at UTC midnight, written in an offset.
        ("2026-03-31T16:00:00-08:00", "2026-04-01T06:00:00Z", [("2026-04-01", 6.0)], 6.0),
        # A start without an offset (UTC) and an end with one (10:00Z).
        (
            "2026-03-31T20:00:00",
            "2026-04-01T02:00:00-08:00",
            [("2026-03-31", 4.0), ("2026-04-01", 10.0)],
            14.0,
        ),
    ],
)
async def test_report_route_by_day_is_split_at_utc_midnight_for_any_offset(
    admin_client, start, end, by_day, total
):
    await _seed(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC))
    resp = await admin_client.get("/reports/utilization", params={"start": start, "end": end})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_hours"] == total
    assert [(b["day"], b["hours"]) for b in data["by_day"]] == by_day
    assert all(b["reservation_count"] == 1 for b in data["by_day"])
    assert sum(b["hours"] for b in data["by_day"]) == pytest.approx(data["total_hours"], abs=1e-9)


async def test_report_route_hands_utc_bounds_downstream(admin_client):
    """The run-count query and the echoed window see UTC, not the caller's offset."""
    await _seed(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC))
    run_count = AsyncMock(return_value=0)
    with patch("app.routers.reservations.fetch_execution_run_count", new=run_count):
        resp = await admin_client.get("/reports/utilization", params=REPRO)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["window_start"] == "2026-04-01T04:00:00Z"
    assert data["window_end"] == "2026-04-01T12:00:00Z"
    sent_start, sent_end, _auth = run_count.await_args.args
    assert sent_start.isoformat() == "2026-04-01T04:00:00+00:00"
    assert sent_end.isoformat() == "2026-04-01T12:00:00+00:00"


async def test_csv_route_reproduction_matches_json_and_names_utc_dates(admin_client):
    """The CSV has no day section; its user hours equal the JSON total and its file
    name carries the UTC dates (2026-04-01, not the caller's local 2026-03-31)."""
    await _seed(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC))
    json_resp = await admin_client.get("/reports/utilization", params=REPRO)
    csv_resp = await admin_client.get(
        "/reports/utilization.csv", params={**REPRO, "section": "user"}
    )
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-disposition"] == (
        'attachment; filename="utilization-user-2026-04-01-to-2026-04-01.csv"'
    )
    lines = csv_resp.text.splitlines()
    assert lines[1] == f"{USER},alice,8.0000,1"
    assert json_resp.json()["total_hours"] == 8.0


@pytest.fixture
def host_zone_minus_eight(monkeypatch):
    """Run with the process's local zone at a fixed -08:00 (POSIX TZ, no zone files)."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", "XST8")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_report_routes_take_a_naive_bound_as_utc_not_the_host_zone(
    admin_client, host_zone_minus_eight
):
    """datetime.astimezone() reads a naive value in the host's zone; the routes must not."""
    assert datetime(2026, 3, 31, 20, 0).astimezone().utcoffset() == timedelta(hours=-8)
    await _seed(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC))
    params = {"start": "2026-03-31T20:00:00", "end": "2026-04-01T12:00:00"}
    json_resp = await admin_client.get("/reports/utilization", params=params)
    assert json_resp.status_code == 200, json_resp.text
    data = json_resp.json()
    assert data["window_start"] == "2026-03-31T20:00:00Z"
    assert [(b["day"], b["hours"]) for b in data["by_day"]] == [
        ("2026-03-31", 4.0),
        ("2026-04-01", 12.0),
    ]
    csv_resp = await admin_client.get(
        "/reports/utilization.csv", params={**params, "section": "user"}
    )
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-disposition"] == (
        'attachment; filename="utilization-user-2026-03-31-to-2026-04-01.csv"'
    )


# --- functional: bounds near datetime's limits answer 422, never 500 ---

REPORT_ROUTES = ("/reports/utilization", "/reports/utilization.csv")


def _no_utc_equivalent(name: str) -> str:
    return (
        f"{name} must fall between 0001-01-01T00:00:00Z and 9999-12-31T23:59:59.999999Z "
        "once converted to UTC"
    )


@pytest.mark.parametrize("route", REPORT_ROUTES)
@pytest.mark.parametrize(
    ("start", "end", "bad"),
    [
        # The review's two inputs: these answered 422 (the 366-day cap) before the
        # routes converted to UTC, then 500 (OverflowError) until the guard.
        ("0001-01-01T00:00:00+05:00", "2026-04-01T00:00:00Z", "start"),
        ("2026-04-01T00:00:00Z", "9999-12-31T23:00:00-05:00", "end"),
        # One microsecond outside the range at each end, in a short window.
        ("0001-01-01T00:59:59.999999+01:00", "0001-01-02T00:00:00Z", "start"),
        ("9999-12-31T00:00:00Z", "9999-12-31T19:00:00-05:00", "end"),
        # The largest offsets Python admits; start is checked first.
        ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59", "start"),
        ("0001-01-02T00:00:00Z", "9999-12-31T23:59:59-23:59", "end"),
    ],
)
async def test_report_routes_answer_422_for_a_bound_with_no_utc_equivalent(
    admin_client, route, start, end, bad
):
    resp = await admin_client.get(route, params={"start": start, "end": end})
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": _no_utc_equivalent(bad)}


@pytest.mark.parametrize(
    ("start", "end", "seed", "by_day", "total"),
    [
        # A positive offset on the first day landing exactly on 0001-01-01T00:00Z, a
        # negative one on the end (11:00Z).
        (
            "0001-01-01T01:00:00+01:00",
            "0001-01-01T06:00:00-05:00",
            (datetime(1, 1, 1, tzinfo=UTC), datetime(1, 1, 3, tzinfo=UTC)),
            [("0001-01-01", 11.0)],
            11.0,
        ),
        # A positive offset on the start (9999-12-30T20:00Z), a negative one on the
        # end (9999-12-31T18:00Z): the last day's slice is cut at end, not midnight.
        (
            "9999-12-30T22:00:00+02:00",
            "9999-12-31T13:00:00-05:00",
            (datetime(9999, 12, 30, tzinfo=UTC), datetime(9999, 12, 31, 23, 0, tzinfo=UTC)),
            [("9999-12-30", 4.0), ("9999-12-31", 18.0)],
            22.0,
        ),
    ],
)
async def test_report_routes_answer_a_window_at_the_edges_of_the_range(
    admin_client, start, end, seed, by_day, total
):
    await _seed(*seed)
    resp = await admin_client.get("/reports/utilization", params={"start": start, "end": end})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [(b["day"], b["hours"]) for b in data["by_day"]] == by_day
    assert data["total_hours"] == total
    csv_resp = await admin_client.get(
        "/reports/utilization.csv", params={"start": start, "end": end, "section": "user"}
    )
    assert csv_resp.status_code == 200, csv_resp.text
    first, last = by_day[0][0], by_day[-1][0]
    assert csv_resp.headers["content-disposition"] == (
        f'attachment; filename="utilization-user-{first}-to-{last}.csv"'
    )


# Minutes east of UTC; None renders the bound naive (UTC).
_EXTREME_OFFSETS = (None, 0, -60, 60, -300, 300, -14 * 60, 14 * 60, -(23 * 60 + 59), 23 * 60 + 59)


def _extreme_bound(rng: random.Random, low: bool) -> datetime:
    """A wall clock within three days of one end of datetime's range, any offset."""
    if low:
        wall = datetime.min + timedelta(seconds=rng.randrange(0, 3 * 86400))
    else:
        wall = datetime.max - timedelta(seconds=rng.randrange(0, 3 * 86400))
    offset = rng.choice(_EXTREME_OFFSETS)
    return wall if offset is None else wall.replace(tzinfo=timezone(timedelta(minutes=offset)))


def _expected_detail(start: datetime, end: datetime) -> str | None:
    """The out-of-range detail the routes owe for this pair, or None when both convert."""
    for name, value in (("start", start), ("end", end)):
        try:
            if value.tzinfo is not None:
                value.astimezone(UTC)
        except OverflowError:
            return _no_utc_equivalent(name)
    return None


async def test_report_routes_never_answer_5xx_for_extreme_bounds_seeded_sweep(admin_client):
    """Seeded pairs near year 1 and year 9999 in every offset, on both routes.

    Reservations sit on both edge days so an accepted window really splits hours there.
    Every answer is 200 or a 422 with a string detail; a bound with no UTC equivalent
    gets exactly its named detail, and the sample must hold each outcome.
    """
    await _seed(datetime(1, 1, 1, tzinfo=UTC), datetime(1, 1, 4, tzinfo=UTC))
    await _seed(datetime(9999, 12, 29, tzinfo=UTC), datetime(9999, 12, 31, 23, 0, tzinfo=UTC))
    rng = random.Random(15232)
    outcomes = {"ok": 0, "no_utc_equivalent": 0, "other_422": 0}
    edge_days = {"0001-01-01": 0, "9999-12-31": 0}
    for i in range(240):
        low = rng.random() < 0.5
        start = _extreme_bound(rng, low)
        # Mostly the same edge (a window of up to three days, either order); sometimes
        # the other edge (far over the 366-day cap) or a bound in an ordinary year.
        pick = rng.random()
        if pick < 0.7:
            end = _extreme_bound(rng, low)
        elif pick < 0.85:
            end = _extreme_bound(rng, not low)
        else:
            end = datetime(2026, 4, 1, tzinfo=_tz(rng.choice((-8, 0, 5))))
        route = REPORT_ROUTES[i % 2]
        params = {"start": start.isoformat(), "end": end.isoformat()}
        resp = await admin_client.get(route, params=params)
        assert resp.status_code in (200, 422), (route, params, resp.status_code, resp.text)
        expected = _expected_detail(start, end)
        if expected is not None:
            assert resp.status_code == 422, (route, params, resp.text)
            assert resp.json() == {"detail": expected}, (route, params)
            outcomes["no_utc_equivalent"] += 1
        elif resp.status_code == 422:
            assert isinstance(resp.json()["detail"], str), (route, params, resp.text)
            outcomes["other_422"] += 1
        else:
            outcomes["ok"] += 1
            if route == REPORT_ROUTES[0]:
                days = {b["day"] for b in resp.json()["by_day"]}
                edge_days["0001-01-01"] += "0001-01-01" in days
                edge_days["9999-12-31"] += "9999-12-31" in days
    assert all(count >= 20 for count in outcomes.values()), outcomes
    # Accepted windows really split hours on the first and the last representable day.
    assert all(count >= 3 for count in edge_days.values()), edge_days
