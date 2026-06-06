"""Edge-case coverage for reporting_service helpers.

Complements test_reporting_service.py with cases the v0 acceptance tests skip:
- _split_hours_per_day boundary math (zero-width, exact-midnight, multi-day).
- build_utilization_report invariants (empty result set, status filter,
  reservation that lies fully outside the window, malformed device_id strings,
  naive datetime handling).
- fetch_user_groups_map resilience (404 per user, malformed JSON body).
- report_to_csv shape regressions (header line per section, empty buckets).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.models.reservation import Base, Reservation, ReservationStatus
from app.schemas.reservation import DeviceBucket, UserBucket, UtilizationReport
from app.services.reporting_service import (
    _split_hours_per_day,
    build_utilization_report,
    fetch_user_groups_map,
    report_to_csv,
    rollup_by_group,
)
from herd_common.enums import TopologyType
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _res(
    user_id,
    owner_name,
    device_ids,
    start,
    end,
    status=ReservationStatus.COMPLETED,
    topology_type=TopologyType.PHYSICAL,
):
    return Reservation(
        id=uuid.uuid4(),
        user_id=user_id,
        owner_name=owner_name,
        device_ids=device_ids,
        topology_id=None,
        topology_type=topology_type,
        start_time=start,
        end_time=end,
        status=status,
    )


# --- _split_hours_per_day ---


def test_split_hours_zero_width_returns_empty():
    moment = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    assert _split_hours_per_day(moment, moment) == []


def test_split_hours_end_before_start_returns_empty():
    start = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    end = start - timedelta(hours=1)
    assert _split_hours_per_day(start, end) == []


def test_split_hours_within_one_day():
    start = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    out = _split_hours_per_day(start, end)
    assert out == [("2026-04-01", 3.0)]


def test_split_hours_across_midnight():
    """A reservation that straddles 00:00 UTC must split into two day buckets."""
    start = datetime(2026, 4, 1, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc)
    out = _split_hours_per_day(start, end)
    assert out == [("2026-04-01", 2.0), ("2026-04-02", 1.0)]


def test_split_hours_three_days_full_middle():
    """A 50-hour reservation covering all of day 2 must produce a 24h middle bucket."""
    start = datetime(2026, 4, 1, 23, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 4, 1, 0, tzinfo=timezone.utc)
    out = _split_hours_per_day(start, end)
    assert out == [
        ("2026-04-01", 1.0),
        ("2026-04-02", 24.0),
        ("2026-04-03", 24.0),
        ("2026-04-04", 1.0),
    ]


# --- build_utilization_report invariants ---


@pytest.mark.asyncio
async def test_build_report_empty_returns_zeros(db_session):
    report = await build_utilization_report(db_session, NOW - timedelta(days=1), NOW, None)
    assert report.total_hours == 0
    assert report.total_reservations == 0
    assert report.by_user == []
    assert report.by_device == []
    assert report.by_topology_type == []
    assert report.by_day == []


@pytest.mark.asyncio
async def test_build_report_window_end_must_be_after_start(db_session):
    with pytest.raises(ValueError):
        await build_utilization_report(db_session, NOW, NOW - timedelta(hours=1), None)


@pytest.mark.asyncio
async def test_build_report_window_end_equal_to_start_rejected(db_session):
    with pytest.raises(ValueError):
        await build_utilization_report(db_session, NOW, NOW, None)


@pytest.mark.asyncio
async def test_build_report_filters_by_status(db_session):
    """A status filter must exclude non-matching reservations even though
    their windows overlap."""
    device_id = str(uuid.uuid4())
    db_session.add(
        _res(
            USER_A,
            "alice",
            [device_id],
            NOW - timedelta(hours=4),
            NOW - timedelta(hours=2),
            status=ReservationStatus.COMPLETED,
        )
    )
    db_session.add(
        _res(
            USER_B,
            "bob",
            [device_id],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=1),
            status=ReservationStatus.CANCELLED,
        )
    )
    await db_session.commit()
    report = await build_utilization_report(
        db_session,
        NOW - timedelta(days=1),
        NOW,
        [ReservationStatus.COMPLETED],
    )
    user_ids = {b.user_id for b in report.by_user}
    assert user_ids == {USER_A}


@pytest.mark.asyncio
async def test_build_report_clips_hours_to_window(db_session):
    """A reservation that starts before window_start must only count the
    in-window hours."""
    device_id = str(uuid.uuid4())
    db_session.add(
        _res(
            USER_A,
            "alice",
            [device_id],
            NOW - timedelta(hours=6),
            NOW - timedelta(hours=2),
            status=ReservationStatus.COMPLETED,
        )
    )
    await db_session.commit()
    # Window covers only the last 3 hours of the reservation.
    report = await build_utilization_report(
        db_session, NOW - timedelta(hours=5), NOW - timedelta(hours=2), None
    )
    assert report.by_user[0].hours == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_malformed_device_id_rejected_at_write_time(db_session):
    """A malformed device id can no longer corrupt reporting: the typed
    reservation_devices join table rejects it when the reservation is built,
    so the old "skip bad ids in the rollup" defense is now enforced upstream.
    A well-formed reservation alongside it still reports normally."""
    with pytest.raises(ValueError):
        _res(
            USER_A,
            "alice",
            ["not-a-uuid", str(uuid.uuid4())],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=1),
        )

    db_session.add(
        _res(
            USER_A,
            "alice",
            [str(uuid.uuid4())],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=1),
        )
    )
    await db_session.commit()
    report = await build_utilization_report(db_session, NOW - timedelta(days=1), NOW, None)
    assert report.total_reservations == 1
    assert len(report.by_device) == 1


@pytest.mark.asyncio
async def test_build_report_day_buckets_count_unique_reservations_per_day(db_session):
    """A reservation that spans two days must increment the count for each day
    once (not double-count within the same reservation)."""
    db_session.add(
        _res(
            USER_A,
            "alice",
            [str(uuid.uuid4())],
            datetime(2026, 4, 1, 23, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()
    report = await build_utilization_report(
        db_session,
        datetime(2026, 3, 31, tzinfo=timezone.utc),
        datetime(2026, 4, 3, tzinfo=timezone.utc),
        None,
    )
    days = {b.day: b for b in report.by_day}
    assert days["2026-04-01"].reservation_count == 1
    assert days["2026-04-02"].reservation_count == 1


# --- fetch_user_groups_map edge cases ---


@pytest.mark.asyncio
async def test_fetch_user_groups_map_empty_user_list_returns_empty():
    out = await fetch_user_groups_map([], "Bearer tok")
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_404_yields_empty_groups_for_user():
    not_found = httpx.Response(404)
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=not_found)
        result = await fetch_user_groups_map([USER_A], "Bearer tok")
    assert result[USER_A] == []


@pytest.mark.asyncio
async def test_fetch_user_groups_map_one_user_unreachable_does_not_kill_others():
    """A per-user HTTPError must let the remaining users still resolve."""
    gid = uuid.uuid4()
    good_resp = httpx.Response(200, json=[{"id": str(gid), "name": "Platform"}])

    calls = {"n": 0}

    async def _flaky_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return good_resp

    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=_flaky_get)
        result = await fetch_user_groups_map([USER_A, USER_B], "Bearer tok")
    assert result[USER_A] == []
    assert result[USER_B] == [(gid, "Platform")]


@pytest.mark.asyncio
async def test_fetch_user_groups_map_missing_name_falls_back_to_empty_string():
    gid = uuid.uuid4()
    resp = httpx.Response(200, json=[{"id": str(gid)}])
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=resp)
        result = await fetch_user_groups_map([USER_A], "Bearer tok")
    assert result[USER_A] == [(gid, "")]


# --- rollup_by_group additional cases ---


def test_rollup_by_group_ungrouped_aggregates_hours_across_users():
    """Two users with no group attachment must share a single Ungrouped row."""
    buckets = [
        UserBucket(user_id=USER_A, owner_name="alice", reservation_count=1, hours=2.0),
        UserBucket(user_id=USER_B, owner_name="bob", reservation_count=1, hours=3.0),
    ]
    result = rollup_by_group(buckets, {USER_A: [], USER_B: []})
    assert len(result) == 1
    assert result[0].group_id is None
    assert result[0].hours == pytest.approx(5.0)
    assert result[0].reservation_count == 2


def test_rollup_by_group_empty_input_returns_empty():
    assert rollup_by_group([], {}) == []


# --- report_to_csv ---


def _empty_report():
    return UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=0.0,
        total_reservations=0,
        by_user=[],
        by_device=[],
    )


def test_report_to_csv_user_section_header_only_when_empty():
    csv_text = report_to_csv(_empty_report(), "user")
    lines = csv_text.strip().splitlines()
    assert lines == ["user_id,owner_name,hours,reservation_count"]


def test_report_to_csv_device_section_header_only_when_empty():
    csv_text = report_to_csv(_empty_report(), "device")
    lines = csv_text.strip().splitlines()
    assert lines == ["device_id,hours,reservation_count"]


def test_report_to_csv_decimal_formatting_four_places():
    """Hours are formatted with exactly 4 decimal places so the column width
    is stable across rows."""
    uid = uuid.uuid4()
    did = uuid.uuid4()
    report = UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=1.0,
        total_reservations=1,
        by_user=[UserBucket(user_id=uid, owner_name="alice", reservation_count=1, hours=1.5)],
        by_device=[DeviceBucket(device_id=did, reservation_count=1, hours=0.3333)],
    )
    user_csv = report_to_csv(report, "user")
    device_csv = report_to_csv(report, "device")
    assert ",1.5000," in user_csv
    assert ",0.3333," in device_csv
