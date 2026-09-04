"""Unit tests for transit-gear inheritance in the device rollups (issue #646
phase 3, ADR 0013 "Delivery phases" point 3, D1-D3).

Exercises build_utilization_report's by_device / by_device_purpose folding of
cabling's fork-connection device union, and the chunking / fail-closed
behavior of the cabling batch call. Cabling itself is always mocked here: the
low-level transport is `app.services.reporting_service._cabling_fork_devices_batch`
and the chunk-and-merge layer above it is `_fetch_transit_devices`; the
directory-wide autouse fixture in tests/conftest.py stubs `_fetch_transit_devices`
to return {} by default, so every test below overrides it (or the lower-level
transport) explicitly for the behavior it wants to prove.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.models.reservation import Base, Reservation, ReservationStatus
from app.services.reporting_service import TransitGearUnavailable, build_utilization_report
from app.services.reporting_service import _fetch_transit_devices as _real_fetch_transit_devices
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


def _reservation(
    user_id: uuid.UUID,
    owner_name: str,
    device_ids: list[str],
    start: datetime,
    end: datetime,
    status: ReservationStatus = ReservationStatus.COMPLETED,
    purpose_category: str | None = None,
) -> Reservation:
    return Reservation(
        id=uuid.uuid4(),
        user_id=user_id,
        owner_name=owner_name,
        device_ids=device_ids,
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=start,
        end_time=end,
        status=status,
        purpose_category=purpose_category,
    )


def _patch_transit(mapping: dict) -> "patch":
    """Patch _fetch_transit_devices to return the given reservation_id -> ids map."""
    return patch(
        "app.services.reporting_service._fetch_transit_devices",
        new=AsyncMock(return_value=mapping),
    )


@pytest.mark.asyncio
async def test_transit_device_inherits_category_and_hours(db_session):
    """One reservation, category training, reserved device A, fork devices {A, S}."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({r.id: [device_a, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].hours == pytest.approx(2.0)
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)

    assert by_device[switch].reservation_count == 1
    assert by_device[switch].hours == pytest.approx(2.0)
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].transit_hours == pytest.approx(2.0)

    by_device_purpose = {(b.device_id, b.purpose_category): b for b in report.by_device_purpose}
    a_bucket = by_device_purpose[(device_a, "training")]
    assert a_bucket.reservations == 1
    assert a_bucket.device_hours == pytest.approx(2.0)
    assert a_bucket.transit_reservations == 0
    assert a_bucket.transit_device_hours == pytest.approx(0.0)

    s_bucket = by_device_purpose[(switch, "training")]
    assert s_bucket.reservations == 1
    assert s_bucket.device_hours == pytest.approx(2.0)
    assert s_bucket.transit_reservations == 1
    assert s_bucket.transit_device_hours == pytest.approx(2.0)
    assert report.transit_included is True


@pytest.mark.asyncio
async def test_reserved_and_transit_dedupes_to_reserved_only(db_session):
    """A device both reserved and on the fork's path counts once, as reserved."""
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    # The fork touched device_a too (it is, after all, the endpoint the
    # reservation reserved), so the union is just {device_a}: nothing left
    # over once the reserved set is subtracted.
    with _patch_transit({r.id: [device_a]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_duplicate_device_ids_in_fork_response_count_once(db_session):
    """Many hops touching one switch must not inflate its transit_reservations."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    # Simulates three hops through the same switch: cabling's own endpoint
    # already dedupes, but the reporting layer must not assume that and
    # re-count per occurrence.
    with _patch_transit({r.id: [device_a, switch, switch, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].transit_hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_mixed_reserved_in_one_transit_in_another(db_session):
    """S reserved in reservation 1, transit in reservation 2, different categories."""
    switch = uuid.uuid4()
    device_b = uuid.uuid4()
    r1 = _reservation(
        USER_A,
        "alice",
        [str(switch)],
        NOW - timedelta(hours=4),
        NOW - timedelta(hours=3),
        purpose_category="training",
    )
    r2 = _reservation(
        USER_B,
        "bob",
        [str(device_b)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="qa_regression",
    )
    db_session.add_all([r1, r2])
    await db_session.commit()

    with _patch_transit({r1.id: [switch], r2.id: [device_b, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].reservation_count == 2
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].hours == pytest.approx(3.0)  # 1h reserved + 2h transit
    assert by_device[switch].transit_hours == pytest.approx(2.0)

    by_device_purpose = {(b.device_id, b.purpose_category): b for b in report.by_device_purpose}
    training_row = by_device_purpose[(switch, "training")]
    assert training_row.reservations == 1
    assert training_row.transit_reservations == 0
    qa_row = by_device_purpose[(switch, "qa_regression")]
    assert qa_row.reservations == 1
    assert qa_row.transit_reservations == 1
    assert qa_row.transit_device_hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_reservation_absent_from_cabling_map_has_no_transit(db_session):
    """A reservation with no fork (absent from the response map) reports reserved-only."""
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_include_transit_false_skips_the_cabling_call(db_session):
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    fetch_mock = AsyncMock(return_value={r.id: [device_a, switch]})
    with patch("app.services.reporting_service._fetch_transit_devices", new=fetch_mock):
        report = await build_utilization_report(
            db_session,
            NOW - timedelta(days=1),
            NOW,
            [ReservationStatus.COMPLETED],
            include_transit=False,
        )

    fetch_mock.assert_not_called()
    assert report.transit_included is False
    by_device = {b.device_id: b for b in report.by_device}
    assert switch not in by_device
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_ai_suggested_only_reservation_contributes_no_device_purpose_transit(db_session):
    """A transit device follows the SAME is_confirmed gate as a reserved one:
    an ai_suggested-but-unconfirmed reservation folds into by_device's transit
    fields but never into by_device_purpose (mirroring the reserved-device rule)."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = Reservation(
        id=uuid.uuid4(),
        user_id=USER_A,
        owner_name="alice",
        device_ids=[str(device_a)],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=NOW - timedelta(hours=2),
        end_time=NOW,
        status=ReservationStatus.COMPLETED,
        purpose_category=None,
        purpose_suggestion={"top_category": "training", "distribution": []},
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({r.id: [device_a, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].transit_reservations == 1
    assert not any(b.device_id == switch for b in report.by_device_purpose)


# --- Chunking and fail-closed behavior -------------------------------------------
#
# These three tests exercise _fetch_transit_devices itself (the chunk-and-merge
# layer the rest of this file stubs out), so each one first un-stubs it back to
# the real implementation for its own body, then mocks the lower-level
# transport, _cabling_fork_devices_batch, instead.


def _real_transit_lookup():
    return patch(
        "app.services.reporting_service._fetch_transit_devices",
        new=_real_fetch_transit_devices,
    )


@pytest.mark.asyncio
async def test_chunking_splits_into_500_and_1(db_session):
    reservations = []
    now_hours = NOW - timedelta(minutes=1)
    for _ in range(501):
        r = _reservation(
            USER_A,
            "alice",
            [],
            now_hours - timedelta(hours=1),
            now_hours,
            purpose_category="other",
        )
        reservations.append(r)
    db_session.add_all(reservations)
    await db_session.commit()

    call_sizes: list[int] = []

    async def _fake_batch(reservation_ids):
        call_sizes.append(len(reservation_ids))
        return httpx.Response(200, json={"devices": {}})

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(side_effect=_fake_batch),
        ),
    ):
        await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    assert call_sizes == [500, 1]


@pytest.mark.asyncio
async def test_transport_failure_raises_transit_gear_unavailable(db_session):
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service: boom")),
        ),
    ):
        with pytest.raises(TransitGearUnavailable):
            await build_utilization_report(
                db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
            )


@pytest.mark.asyncio
async def test_non_200_raises_transit_gear_unavailable(db_session):
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(return_value=httpx.Response(500, text="boom")),
        ),
    ):
        with pytest.raises(TransitGearUnavailable):
            await build_utilization_report(
                db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
            )
