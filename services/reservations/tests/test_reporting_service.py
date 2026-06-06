"""Direct unit tests for app.services.reporting_service.build_utilization_report."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.models.reservation import Base, Reservation, ReservationStatus
from app.schemas.reservation import DeviceBucket, UserBucket, UtilizationReport
from app.services.reporting_service import (
    build_utilization_report,
    fetch_execution_run_count,
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
DEVICE_X = str(uuid.uuid4())
DEVICE_Y = str(uuid.uuid4())


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _reservation(
    user_id: uuid.UUID,
    owner_name: str,
    device_ids: list[str],
    start: datetime,
    end: datetime,
    status: ReservationStatus = ReservationStatus.COMPLETED,
    topology_type: TopologyType = TopologyType.PHYSICAL,
) -> Reservation:
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


@pytest.mark.asyncio
async def test_build_report_aggregates_users_and_devices(db_session):
    # Alice: 3h on device_x
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=4),
            NOW - timedelta(hours=1),
        )
    )
    # Bob: 2h across two devices
    db_session.add(
        _reservation(
            USER_B,
            "bob",
            [DEVICE_X, DEVICE_Y],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=1),
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )

    assert report.total_reservations == 2
    assert report.total_hours == pytest.approx(5.0, abs=0.01)
    # Alice first (3h), Bob second (2h)
    assert report.by_user[0].owner_name == "alice"
    assert report.by_user[0].hours == pytest.approx(3.0, abs=0.01)
    assert report.by_user[1].owner_name == "bob"
    by_device = {str(b.device_id): b for b in report.by_device}
    assert by_device[DEVICE_X].hours == pytest.approx(5.0, abs=0.01)
    assert by_device[DEVICE_Y].hours == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_build_report_clamps_window(db_session):
    # Reservation spans outside window; only the inside portion counts.
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=10),
            NOW + timedelta(hours=2),
        )
    )
    await db_session.commit()

    window_start = NOW - timedelta(hours=3)
    window_end = NOW
    report = await build_utilization_report(
        db_session, window_start, window_end, [ReservationStatus.COMPLETED]
    )
    assert report.total_hours == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_build_report_filters_status(db_session):
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            status=ReservationStatus.CANCELLED,
        )
    )
    db_session.add(
        _reservation(
            USER_B,
            "bob",
            [DEVICE_X],
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            status=ReservationStatus.COMPLETED,
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )
    assert [b.owner_name for b in report.by_user] == ["bob"]

    report_cancelled = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.CANCELLED]
    )
    assert [b.owner_name for b in report_cancelled.by_user] == ["alice"]


@pytest.mark.asyncio
async def test_build_report_skips_zero_hours(db_session):
    # Reservation ends exactly at window_start; clamped duration is 0.
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=10),
            NOW - timedelta(hours=5),
        )
    )
    await db_session.commit()
    report = await build_utilization_report(
        db_session, NOW - timedelta(hours=5), NOW, [ReservationStatus.COMPLETED]
    )
    assert report.total_reservations == 0
    assert report.by_user == []
    assert report.by_device == []


@pytest.mark.asyncio
async def test_invalid_device_id_rejected_at_write_time(db_session):
    # A non-UUID device id is now rejected when the reservation is built (the
    # reservation_devices column is typed), so invalid ids can never reach the
    # reporting rollup in the first place.
    with pytest.raises(ValueError):
        _reservation(
            USER_A,
            "alice",
            ["not-a-uuid"],
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_build_report_rejects_inverted_window(db_session):
    with pytest.raises(ValueError):
        await build_utilization_report(
            db_session, NOW, NOW - timedelta(hours=1), [ReservationStatus.COMPLETED]
        )


@pytest.mark.asyncio
async def test_build_report_returns_empty_when_no_reservations(db_session):
    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )
    assert report.total_hours == 0.0
    assert report.total_reservations == 0
    assert report.by_user == []
    assert report.by_device == []
    assert report.by_topology_type == []
    assert report.execution_run_count is None


@pytest.mark.asyncio
async def test_build_report_aggregates_by_day_across_midnight(db_session):
    # Reservation that spans 20h, crossing one UTC midnight boundary
    start = datetime(2026, 4, 1, 20, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 2, 16, 0, tzinfo=timezone.utc)
    db_session.add(_reservation(USER_A, "alice", [DEVICE_X], start, end))
    await db_session.commit()

    report = await build_utilization_report(
        db_session,
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 3, tzinfo=timezone.utc),
        [ReservationStatus.COMPLETED],
    )
    by_day = {b.day: b for b in report.by_day}
    assert by_day["2026-04-01"].hours == pytest.approx(4.0, abs=0.01)
    assert by_day["2026-04-02"].hours == pytest.approx(16.0, abs=0.01)
    assert by_day["2026-04-01"].reservation_count == 1
    assert by_day["2026-04-02"].reservation_count == 1
    # Days are in ascending chronological order.
    assert [b.day for b in report.by_day] == ["2026-04-01", "2026-04-02"]


@pytest.mark.asyncio
async def test_build_report_aggregates_by_topology_type(db_session):
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=1),
            topology_type=TopologyType.PHYSICAL,
        )
    )
    db_session.add(
        _reservation(
            USER_B,
            "bob",
            [DEVICE_Y],
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            topology_type=TopologyType.CLOUD,
        )
    )
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=5),
            NOW - timedelta(hours=4),
            topology_type=TopologyType.PHYSICAL,
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )
    by_type = {b.topology_type: b for b in report.by_topology_type}
    assert by_type[TopologyType.PHYSICAL].hours == pytest.approx(3.0, abs=0.01)
    assert by_type[TopologyType.PHYSICAL].reservation_count == 2
    assert by_type[TopologyType.CLOUD].hours == pytest.approx(1.0, abs=0.01)
    assert by_type[TopologyType.CLOUD].reservation_count == 1
    # Sorted by hours desc: PHYSICAL first
    assert report.by_topology_type[0].topology_type == TopologyType.PHYSICAL


def _report_fixture() -> UtilizationReport:
    uid = uuid.uuid4()
    did = uuid.uuid4()
    return UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=5.0,
        total_reservations=2,
        by_user=[
            UserBucket(user_id=uid, owner_name="alice", reservation_count=2, hours=5.0),
        ],
        by_device=[
            DeviceBucket(device_id=did, reservation_count=2, hours=5.0),
        ],
    )


def test_report_to_csv_user_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "user")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "user_id,owner_name,hours,reservation_count"
    assert lines[1].endswith(",alice,5.0000,2")


def test_report_to_csv_device_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "device")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "device_id,hours,reservation_count"
    assert lines[1].endswith(",5.0000,2")


def test_report_to_csv_rejects_unknown_section():
    with pytest.raises(ValueError):
        report_to_csv(_report_fixture(), "template")


@pytest.mark.asyncio
async def test_fetch_execution_run_count_returns_total():
    mock_resp = httpx.Response(200, json={"items": [], "total": 7, "skip": 0, "limit": 1})
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=mock_resp)
        count = await fetch_execution_run_count(NOW - timedelta(days=1), NOW, "Bearer tok")
    assert count == 7


@pytest.mark.asyncio
async def test_fetch_execution_run_count_returns_none_when_unauthenticated():
    count = await fetch_execution_run_count(NOW - timedelta(days=1), NOW, None)
    assert count is None


@pytest.mark.asyncio
async def test_fetch_execution_run_count_swallows_http_errors():
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        count = await fetch_execution_run_count(NOW - timedelta(days=1), NOW, "Bearer tok")
    assert count is None


@pytest.mark.asyncio
async def test_fetch_execution_run_count_returns_none_on_non_200():
    mock_resp = httpx.Response(503, text="upstream down")
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=mock_resp)
        count = await fetch_execution_run_count(NOW - timedelta(days=1), NOW, "Bearer tok")
    assert count is None


def test_rollup_by_group_buckets_users_into_groups():
    gid_a = uuid.uuid4()
    gid_b = uuid.uuid4()
    buckets = [
        UserBucket(user_id=USER_A, owner_name="alice", reservation_count=3, hours=5.0),
        UserBucket(user_id=USER_B, owner_name="bob", reservation_count=1, hours=2.0),
    ]
    user_groups = {
        USER_A: [(gid_a, "Platform")],
        USER_B: [(gid_b, "QA")],
    }
    result = rollup_by_group(buckets, user_groups)
    by_name = {b.group_name: b for b in result}
    assert by_name["Platform"].hours == pytest.approx(5.0)
    assert by_name["Platform"].reservation_count == 3
    assert by_name["QA"].hours == pytest.approx(2.0)
    # Ordered by hours desc
    assert result[0].group_name == "Platform"


def test_rollup_by_group_counts_multi_group_user_against_each_group():
    gid_a = uuid.uuid4()
    gid_b = uuid.uuid4()
    buckets = [UserBucket(user_id=USER_A, owner_name="alice", reservation_count=2, hours=4.0)]
    user_groups = {USER_A: [(gid_a, "Platform"), (gid_b, "On-call")]}
    result = rollup_by_group(buckets, user_groups)
    # Same hours attributed to both groups (intentional double-count)
    assert {b.group_name for b in result} == {"Platform", "On-call"}
    assert all(b.hours == pytest.approx(4.0) for b in result)


def test_rollup_by_group_ungrouped_bucket_for_usersless_without_groups():
    buckets = [UserBucket(user_id=USER_A, owner_name="alice", reservation_count=1, hours=3.0)]
    result = rollup_by_group(buckets, {USER_A: []})
    assert len(result) == 1
    assert result[0].group_id is None
    assert result[0].group_name == "Ungrouped"
    assert result[0].hours == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_fetch_user_groups_map_returns_empty_without_auth():
    out = await fetch_user_groups_map([USER_A], None)
    assert out == {USER_A: []}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_collects_groups_per_user():
    gid = uuid.uuid4()
    ok_resp = httpx.Response(200, json=[{"id": str(gid), "name": "Platform"}])
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=ok_resp)
        result = await fetch_user_groups_map([USER_A], "Bearer tok")
    assert result[USER_A] == [(gid, "Platform")]


def test_report_to_csv_handles_commas_in_owner_name():
    uid = uuid.uuid4()
    did = uuid.uuid4()
    report = UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=1.0,
        total_reservations=1,
        by_user=[
            UserBucket(user_id=uid, owner_name="Doe, Jane", reservation_count=1, hours=1.0),
        ],
        by_device=[DeviceBucket(device_id=did, reservation_count=1, hours=1.0)],
    )
    csv_text = report_to_csv(report, "user")
    assert '"Doe, Jane"' in csv_text
