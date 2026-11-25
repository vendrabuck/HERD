"""Edge-case coverage for reporting_service helpers.

Complements test_reporting_service.py with cases the v0 acceptance tests skip:
- _split_hours_per_day boundary math (zero-width, exact-midnight, multi-day).
- build_utilization_report invariants (empty result set, status filter,
  reservation that lies fully outside the window, malformed device_id strings,
  naive datetime handling).
- What the report query loads: rows that overlap the window, in a counted status.
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
from sqlalchemy import event
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
@pytest.mark.parametrize(
    ("start_offset", "end_offset"),
    [
        (timedelta(hours=-3), timedelta(0)),
        (timedelta(hours=24), timedelta(hours=27)),
        (timedelta(hours=5), timedelta(hours=5)),
        (timedelta(days=-30), timedelta(days=-29)),
        (timedelta(days=30), timedelta(days=31)),
    ],
    ids=[
        "ends-at-window-start",
        "starts-at-window-end",
        "zero-length-inside",
        "far-before",
        "far-after",
    ],
)
async def test_a_row_with_no_overlap_is_skipped(db_session, start_offset, end_offset):
    """OPS-REPORT-2: "one with no overlap is skipped". Beside one counted row, a row
    that overlaps the 24-hour window for no time at all (far before or after it,
    touching either bound, or of zero length inside it) shows in none of the totals or
    the user, device, topology type, day, and purpose sections. The two rows have
    different users and devices."""
    window_start = NOW - timedelta(days=1)
    window_end = NOW
    counted_device = uuid.uuid4()
    skipped_device = uuid.uuid4()
    db_session.add(
        _res(
            USER_A,
            "alice",
            [str(counted_device)],
            window_start + timedelta(hours=1),
            window_start + timedelta(hours=3),
        )
    )
    db_session.add(
        _res(
            USER_B,
            "bob",
            [str(skipped_device)],
            window_start + start_offset,
            window_start + end_offset,
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, window_start, window_end, [ReservationStatus.COMPLETED]
    )

    assert report.total_reservations == 1
    assert report.total_hours == pytest.approx(2.0)
    assert [(b.user_id, b.hours, b.reservation_count) for b in report.by_user] == [(USER_A, 2.0, 1)]
    assert [(b.device_id, b.hours) for b in report.by_device] == [(counted_device, 2.0)]
    assert [(b.topology_type, b.reservation_count) for b in report.by_topology_type] == [
        (TopologyType.PHYSICAL, 1)
    ]
    assert [(b.day, b.hours) for b in report.by_day] == [
        ((window_start + timedelta(hours=1)).date().isoformat(), 2.0)
    ]
    assert [(b.purpose_category, b.reservations) for b in report.by_purpose] == [
        ("unclassified", 1)
    ]


async def _loaded_reservations(db_session, *args, **kwargs) -> list[Reservation]:
    """Run build_utilization_report and return the Reservation rows its query loaded.

    The session forgets every row first, so each row the query returns is loaded fresh
    and fires the ORM `load` event. The report skips a row with no overlap in Python,
    so what the query loads is not visible in the report itself."""
    db_session.expunge_all()
    loaded: list[Reservation] = []

    def _on_load(target, _context):
        loaded.append(target)

    event.listen(Reservation, "load", _on_load)
    try:
        await build_utilization_report(db_session, *args, **kwargs)
    finally:
        event.remove(Reservation, "load", _on_load)
    return loaded


@pytest.mark.asyncio
async def test_the_report_query_loads_only_rows_that_overlap_the_window(db_session):
    """OPS-REPORT-2: the report query loads only reservations that end after the
    window's start and start before its end, and loads a zero-length one inside the
    window (which the report then skips). Loaded: rows inside, of zero length inside,
    across either bound, and across the whole window. Not loaded: a row ending exactly
    at the start, one starting exactly at the end, and one far before and one far after
    the window. Every row is COMPLETED, the status asked for."""
    window_start = NOW - timedelta(days=1)
    window_end = NOW

    def _row(start, end):
        return _res(USER_A, "alice", [str(uuid.uuid4())], start, end)

    hour = timedelta(hours=1)
    loaded_rows = [
        _row(window_start + hour, window_start + 2 * hour),
        _row(window_start + 5 * hour, window_start + 5 * hour),
        _row(window_start - hour, window_start + hour),
        _row(window_end - hour, window_end + hour),
        _row(window_start - hour, window_end + hour),
    ]
    skipped_rows = [
        _row(window_start - hour, window_start),
        _row(window_end, window_end + hour),
        _row(window_start - timedelta(days=30), window_start - timedelta(days=29)),
        _row(window_end + timedelta(days=29), window_end + timedelta(days=30)),
    ]
    db_session.add_all([*loaded_rows, *skipped_rows])
    await db_session.commit()

    loaded = await _loaded_reservations(
        db_session, window_start, window_end, [ReservationStatus.COMPLETED]
    )

    assert sorted(str(r.id) for r in loaded) == sorted(str(r.id) for r in loaded_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_filter", "fleet_status_filter", "fleet_devices", "expected"),
    [
        ([ReservationStatus.COMPLETED], None, None, {ReservationStatus.COMPLETED}),
        (
            [ReservationStatus.COMPLETED],
            [ReservationStatus.ACTIVE, ReservationStatus.COMPLETED],
            None,
            {ReservationStatus.COMPLETED},
        ),
        (
            [ReservationStatus.COMPLETED],
            [ReservationStatus.ACTIVE, ReservationStatus.COMPLETED],
            [],
            {ReservationStatus.ACTIVE, ReservationStatus.COMPLETED},
        ),
        (
            [ReservationStatus.PENDING, ReservationStatus.CANCELLED],
            None,
            [],
            {ReservationStatus.PENDING, ReservationStatus.CANCELLED},
        ),
        (None, None, None, set(ReservationStatus)),
    ],
    ids=[
        "user-and-device-sections-only",
        "no-fleet-section",
        "fleet-section-adds-its-statuses",
        "fleet-section-takes-the-same-statuses",
        "every-status-counted",
    ],
)
async def test_the_report_query_loads_only_the_statuses_a_section_counts(
    db_session, status_filter, fleet_status_filter, fleet_devices, expected
):
    """OPS-REPORT-2: the report query loads only reservations in a status the user,
    device, or fleet section counts. One row per status lies inside the window. With no
    inventory list there is no fleet section, so a fleet status list adds nothing; with
    one (empty here), the fleet section counts its own statuses, or the user section's
    when it is given none; with no status list every status is counted and loaded."""
    window_start = NOW - timedelta(days=1)
    for status in ReservationStatus:
        db_session.add(
            _res(
                USER_A,
                "alice",
                [str(uuid.uuid4())],
                window_start + timedelta(hours=1),
                window_start + timedelta(hours=2),
                status=status,
            )
        )
    await db_session.commit()

    loaded = await _loaded_reservations(
        db_session,
        window_start,
        NOW,
        status_filter,
        fleet_status_filter=fleet_status_filter,
        fleet_devices=fleet_devices,
    )

    assert sorted(r.status.value for r in loaded) == sorted(s.value for s in expected)


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
async def test_fetch_user_groups_map_non_200_fails_soft_to_empty():
    """A non-200 from the batch endpoint yields every user empty, not an error."""
    not_found = httpx.Response(404)
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=not_found)
        result = await fetch_user_groups_map([USER_A, USER_B], "Bearer tok")
    assert result == {USER_A: [], USER_B: []}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_transport_error_fails_soft_for_all():
    """A transport error on the single batch call must not fail the report:
    every requested user resolves to an empty membership list."""
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        result = await fetch_user_groups_map([USER_A, USER_B], "Bearer tok")
    assert result == {USER_A: [], USER_B: []}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_missing_name_falls_back_to_empty_string():
    gid = uuid.uuid4()
    resp = httpx.Response(200, json={str(USER_A): [{"id": str(gid)}]})
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=resp)
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
    assert lines == ["device_id,hours,reservation_count,transit_reservations,transit_hours"]


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


@pytest.mark.asyncio
async def test_fetch_user_groups_map_sends_batches_of_auth_s_own_cap():
    """Issue #1466: a report's users are not bounded, so they go to auth in batches of
    500, its route's cap, each body about 20 KB; every user's groups come back."""
    from app.services.reporting_service import USER_GROUPS_BATCH

    users = [uuid.uuid4() for _ in range(1_201)]
    group = {"id": str(uuid.uuid4()), "name": "lab"}
    sent: list[list[str]] = []

    async def post(url, json, headers):
        sent.append(json["user_ids"])
        return httpx.Response(200, json={uid: [group] for uid in json["user_ids"]})

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = post
        result = await fetch_user_groups_map(users, "Bearer tok")
    assert USER_GROUPS_BATCH == 500
    assert [len(batch) for batch in sent] == [500, 500, 201]
    assert result == {uid: [(uuid.UUID(group["id"]), "lab")] for uid in users}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_one_failed_batch_fails_soft_for_all():
    calls = 0

    async def post(url, json, headers):
        nonlocal calls
        calls += 1
        return httpx.Response(200 if calls == 1 else 503, json={})

    users = [uuid.uuid4() for _ in range(600)]
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = post
        result = await fetch_user_groups_map(users, "Bearer tok")
    assert result == {uid: [] for uid in users}
