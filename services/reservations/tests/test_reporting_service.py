"""Direct unit tests for app.services.reporting_service.build_utilization_report."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.models.reservation import Base, Reservation, ReservationStatus
from app.schemas.reservation import (
    DeviceBucket,
    DevicePurposeBucket,
    PurposeBucket,
    UserBucket,
    UserPurposeBucket,
    UtilizationReport,
)
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
    purpose_category: str | None = None,
    purpose_suggestion: dict | None = None,
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
        purpose_category=purpose_category,
        purpose_suggestion=purpose_suggestion,
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
async def test_build_report_span_over_max_raises(db_session, monkeypatch):
    """A window wider than utilization_max_span_days raises ValueError (issue #389)."""
    from app.services import reporting_service as svc

    monkeypatch.setattr(svc.settings, "utilization_max_span_days", 30)
    with pytest.raises(ValueError, match="30 days"):
        await build_utilization_report(
            db_session, NOW, NOW + timedelta(days=31), [ReservationStatus.COMPLETED]
        )


@pytest.mark.asyncio
async def test_build_report_span_at_max_succeeds(db_session, monkeypatch):
    """A window exactly at utilization_max_span_days does not raise."""
    from app.services import reporting_service as svc

    monkeypatch.setattr(svc.settings, "utilization_max_span_days", 30)
    report = await build_utilization_report(
        db_session, NOW, NOW + timedelta(days=30), [ReservationStatus.COMPLETED]
    )
    assert report.total_reservations == 0


@pytest.mark.asyncio
async def test_build_report_span_guard_disabled_when_zero(db_session, monkeypatch):
    """utilization_max_span_days=0 disables the cap entirely."""
    from app.services import reporting_service as svc

    monkeypatch.setattr(svc.settings, "utilization_max_span_days", 0)
    report = await build_utilization_report(
        db_session, NOW, NOW + timedelta(days=3650), [ReservationStatus.COMPLETED]
    )
    assert report.total_reservations == 0


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


@pytest.mark.asyncio
async def test_build_report_aggregates_by_purpose(db_session):
    """by_purpose/by_user_purpose/by_device_purpose (issue #646 phase 1):
    device_hours is actual device-hours (reservation hours times device
    count), and a null purpose_category buckets under the literal
    "unclassified" string, never a null value."""
    # Alice, 2 devices, 2h, classified: 4 device-hours into qa_regression.
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X, DEVICE_Y],
            NOW - timedelta(hours=10),
            NOW - timedelta(hours=8),
            purpose_category="qa_regression",
        )
    )
    # Bob, 1 device, 1h, unclassified.
    db_session.add(
        _reservation(
            USER_B,
            "bob",
            [DEVICE_X],
            NOW - timedelta(hours=6),
            NOW - timedelta(hours=5),
            purpose_category=None,
        )
    )
    # Alice again, 1 device, 1h, same classification: adds 1 more device-hour
    # and one more reservation to the qa_regression bucket.
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_Y],
            NOW - timedelta(hours=4),
            NOW - timedelta(hours=3),
            purpose_category="qa_regression",
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )

    by_purpose = {b.purpose_category: b for b in report.by_purpose}
    assert by_purpose["qa_regression"].reservations == 2
    assert by_purpose["qa_regression"].device_hours == pytest.approx(5.0, abs=0.01)
    assert by_purpose["unclassified"].reservations == 1
    assert by_purpose["unclassified"].device_hours == pytest.approx(1.0, abs=0.01)

    by_user_purpose = {(b.user_id, b.purpose_category): b for b in report.by_user_purpose}
    alice_qa = by_user_purpose[(USER_A, "qa_regression")]
    assert alice_qa.reservations == 2
    assert alice_qa.device_hours == pytest.approx(5.0, abs=0.01)
    bob_unclassified = by_user_purpose[(USER_B, "unclassified")]
    assert bob_unclassified.reservations == 1
    assert bob_unclassified.device_hours == pytest.approx(1.0, abs=0.01)

    device_x_uid = uuid.UUID(DEVICE_X)
    device_y_uid = uuid.UUID(DEVICE_Y)
    by_device_purpose = {(b.device_id, b.purpose_category): b for b in report.by_device_purpose}
    assert by_device_purpose[(device_x_uid, "qa_regression")].reservations == 1
    assert by_device_purpose[(device_x_uid, "qa_regression")].device_hours == pytest.approx(
        2.0, abs=0.01
    )
    assert by_device_purpose[(device_x_uid, "unclassified")].reservations == 1
    assert by_device_purpose[(device_x_uid, "unclassified")].device_hours == pytest.approx(
        1.0, abs=0.01
    )
    assert by_device_purpose[(device_y_uid, "qa_regression")].reservations == 2
    assert by_device_purpose[(device_y_uid, "qa_regression")].device_hours == pytest.approx(
        3.0, abs=0.01
    )


@pytest.mark.asyncio
async def test_build_report_by_purpose_suggested_split(db_session):
    """Three-way split (issue #646 phase 2, ADR 0013 point 9): a row with a
    suggestion but no confirmed category reports under by_purpose_suggested,
    keyed by the suggestion's top_category, and drops out of by_purpose's
    "unclassified" bucket entirely; a confirmed row that ALSO carries a
    suggestion still reports only under its confirmed category, never
    double-counted into by_purpose_suggested."""
    # Bob: 1 device, 2h, no confirmed category, an AI suggestion of "training".
    db_session.add(
        _reservation(
            USER_B,
            "bob",
            [DEVICE_X],
            NOW - timedelta(hours=6),
            NOW - timedelta(hours=4),
            purpose_category=None,
            purpose_suggestion={"top_category": "training"},
        )
    )
    # Alice: 1 device, 1h, genuinely unclassified (no category, no suggestion).
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_Y],
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=2),
            purpose_category=None,
            purpose_suggestion=None,
        )
    )
    # Alice again: confirmed qa_regression, but ALSO carries a (disagreeing)
    # suggestion; must count only under the confirmed category.
    db_session.add(
        _reservation(
            USER_A,
            "alice",
            [DEVICE_X],
            NOW - timedelta(hours=10),
            NOW - timedelta(hours=9),
            purpose_category="qa_regression",
            purpose_suggestion={"top_category": "training"},
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
    )

    by_purpose = {b.purpose_category: b for b in report.by_purpose}
    # Only the genuinely-unclassified row (Alice's second one) lands here.
    assert by_purpose["unclassified"].reservations == 1
    assert by_purpose["unclassified"].device_hours == pytest.approx(1.0, abs=0.01)
    assert by_purpose["qa_regression"].reservations == 1
    assert by_purpose["qa_regression"].device_hours == pytest.approx(1.0, abs=0.01)
    # Bob's suggested-but-unconfirmed row never reaches by_purpose at all.
    assert "training" not in by_purpose

    by_suggested = {b.purpose_category: b for b in report.by_purpose_suggested}
    assert list(by_suggested.keys()) == ["training"]
    assert by_suggested["training"].reservations == 1
    assert by_suggested["training"].device_hours == pytest.approx(2.0, abs=0.01)


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
        by_purpose=[
            PurposeBucket(purpose_category="qa_regression", reservations=2, device_hours=5.0),
        ],
        by_user_purpose=[
            UserPurposeBucket(
                user_id=uid, purpose_category="qa_regression", reservations=2, device_hours=5.0
            ),
        ],
        by_device_purpose=[
            DevicePurposeBucket(
                device_id=did, purpose_category="qa_regression", reservations=2, device_hours=5.0
            ),
        ],
        by_purpose_suggested=[
            PurposeBucket(purpose_category="training", reservations=1, device_hours=2.0),
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
    assert lines[0] == "device_id,hours,reservation_count,transit_reservations,transit_hours"
    assert lines[1].endswith(",5.0000,2,0,0.0000")


def test_report_to_csv_purpose_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "purpose")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "purpose_category,reservations,device_hours"
    assert lines[1] == "qa_regression,2,5.0000"


def test_report_to_csv_user_purpose_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "user_purpose")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "user_id,purpose_category,reservations,device_hours"
    assert lines[1].endswith(",qa_regression,2,5.0000")


def test_report_to_csv_device_purpose_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "device_purpose")
    lines = csv_text.strip().splitlines()
    assert lines[0] == (
        "device_id,purpose_category,reservations,device_hours,"
        "transit_reservations,transit_device_hours"
    )
    assert lines[1].endswith(",qa_regression,2,5.0000,0,0.0000")


def test_report_to_csv_device_section_carries_transit_columns():
    """The device CSV section's new columns (issue #646 phase 3)."""
    did = uuid.uuid4()
    report = UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=5.0,
        total_reservations=2,
        by_user=[],
        by_device=[
            DeviceBucket(
                device_id=did,
                reservation_count=3,
                hours=8.0,
                transit_reservations=1,
                transit_hours=3.0,
            ),
        ],
    )
    csv_text = report_to_csv(report, "device")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "device_id,hours,reservation_count,transit_reservations,transit_hours"
    assert lines[1] == f"{did},8.0000,3,1,3.0000"


def test_report_to_csv_device_purpose_section_carries_transit_columns():
    did = uuid.uuid4()
    report = UtilizationReport(
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        total_hours=5.0,
        total_reservations=2,
        by_user=[],
        by_device=[],
        by_device_purpose=[
            DevicePurposeBucket(
                device_id=did,
                purpose_category="training",
                reservations=3,
                device_hours=8.0,
                transit_reservations=1,
                transit_device_hours=3.0,
            ),
        ],
    )
    csv_text = report_to_csv(report, "device_purpose")
    lines = csv_text.strip().splitlines()
    assert lines[0] == (
        "device_id,purpose_category,reservations,device_hours,"
        "transit_reservations,transit_device_hours"
    )
    assert lines[1] == f"{did},training,3,8.0000,1,3.0000"


def test_report_to_csv_purpose_suggested_section():
    report = _report_fixture()
    csv_text = report_to_csv(report, "purpose_suggested")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "purpose_category,reservations,device_hours"
    assert lines[1] == "training,1,2.0000"


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
async def test_fetch_user_groups_map_uses_single_batch_call():
    gid = uuid.uuid4()
    ok_resp = httpx.Response(
        200,
        json={
            str(USER_A): [{"id": str(gid), "name": "Platform"}],
            str(USER_B): [],
        },
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=ok_resp)
        result = await fetch_user_groups_map([USER_A, USER_B], "Bearer tok")

    # Exactly one round-trip to auth regardless of user count (no N+1 fan-out).
    assert instance.post.await_count == 1
    called_url = instance.post.await_args.args[0]
    assert called_url.endswith("/groups/users/groups")
    assert result[USER_A] == [(gid, "Platform")]
    assert result[USER_B] == []


@pytest.mark.asyncio
async def test_fetch_user_groups_map_fails_soft_on_non_200():
    err_resp = httpx.Response(503, text="auth down")
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=err_resp)
        result = await fetch_user_groups_map([USER_A, USER_B], "Bearer tok")
    # Every requested user still present, bucketed as Ungrouped, no exception.
    assert result == {USER_A: [], USER_B: []}


@pytest.mark.asyncio
async def test_fetch_user_groups_map_fails_soft_on_unreachable():
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        result = await fetch_user_groups_map([USER_A], "Bearer tok")
    assert result == {USER_A: []}


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
