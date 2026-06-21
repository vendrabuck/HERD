"""Unit tests for reservation_service.py and expiration.py (service-layer, no router)."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.reservation import (
    Reservation,
    ReservationDevice,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationCreate, ReservationUpdate
from app.services.reservation_service import (
    _acquire_device_locks,
    _check_conflicts,
    _create_reservation_fork,
    _create_reservation_fork_best_effort,
    _fetch_devices,
    _fetch_devices_best_effort,
    _publish_nats_event,
    _update_device_statuses,
    cancel_reservation,
    create_reservation,
    get_reservation,
    list_calendar_reservations,
    list_user_reservations,
    release_reservation,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)
USER_ID = uuid.uuid4()
DEVICE_A = uuid.uuid4()
DEVICE_B = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _make_device(device_id, topology="PHYSICAL", status="AVAILABLE", exclusive=True):
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "topology_type": topology,
        "status": status,
        "exclusive": exclusive,
    }


async def _insert_reservation(
    db,
    user_id=None,
    device_ids=None,
    status=ReservationStatus.ACTIVE,
    start_offset_h=1,
    end_offset_h=3,
):
    if user_id is None:
        user_id = USER_ID
    if device_ids is None:
        device_ids = [DEVICE_A]
    res = Reservation(
        user_id=user_id,
        device_ids=[str(d) for d in device_ids],
        topology_type=TopologyType.PHYSICAL,
        purpose="test",
        start_time=NOW + timedelta(hours=start_offset_h),
        end_time=NOW + timedelta(hours=end_offset_h),
        status=status,
    )
    db.add(res)
    await db.commit()
    await db.refresh(res)
    return res


# --- _fetch_devices ---


@pytest.mark.asyncio
async def test_fetch_devices_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": str(DEVICE_A), "name": "dev-a"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_devices([DEVICE_A], "fake-token")
    assert len(result) == 1
    assert result[0]["name"] == "dev-a"


@pytest.mark.asyncio
async def test_fetch_devices_404_raises_value_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="not found"):
            await _fetch_devices([DEVICE_A], "fake-token")


@pytest.mark.asyncio
async def test_fetch_devices_server_error_raises():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = Exception("Server Error")

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(Exception, match="Server Error"):
            await _fetch_devices([DEVICE_A], "fake-token")


# --- _check_conflicts ---


@pytest.mark.asyncio
async def test_check_conflicts_no_overlap():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, start_offset_h=1, end_offset_h=3)
        # Check a non-overlapping window
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=5),
            NOW + timedelta(hours=7),
        )
        assert conflicts == []


@pytest.mark.asyncio
async def test_check_conflicts_with_overlap():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, start_offset_h=1, end_offset_h=3)
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=2),
            NOW + timedelta(hours=4),
        )
        assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_check_conflicts_exclude_id():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, start_offset_h=1, end_offset_h=3)
        # Same window but excluding the reservation itself
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=1),
            NOW + timedelta(hours=3),
            exclude_id=res.id,
        )
        assert conflicts == []


@pytest.mark.asyncio
async def test_check_conflicts_exclusive_filter():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        # Only check DEVICE_B as exclusive; DEVICE_A is not in the exclusive set
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=1),
            NOW + timedelta(hours=3),
            exclusive_device_ids={str(DEVICE_B)},
        )
        assert conflicts == []


@pytest.mark.asyncio
async def test_check_conflicts_cancelled_not_conflicting():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, status=ReservationStatus.CANCELLED)
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=1),
            NOW + timedelta(hours=3),
        )
        assert conflicts == []


@pytest.mark.asyncio
async def test_check_conflicts_pending_provision_visible():
    """A PENDING_PROVISION reservation must be seen by a concurrent create's
    conflict check; this is the race-closing invariant the join rows preserve."""
    async with TestSessionLocal() as db:
        await _insert_reservation(
            db,
            device_ids=[DEVICE_A],
            status=ReservationStatus.PENDING_PROVISION,
            start_offset_h=1,
            end_offset_h=3,
        )
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A],
            NOW + timedelta(hours=1),
            NOW + timedelta(hours=3),
            exclusive_device_ids={str(DEVICE_A)},
        )
        assert conflicts == [DEVICE_A]


@pytest.mark.asyncio
async def test_check_conflicts_distinct_devices_across_reservations():
    """Two overlapping reservations on different devices return each conflicting
    device once (the join query is DISTINCT)."""
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        await _insert_reservation(db, device_ids=[DEVICE_B], start_offset_h=1, end_offset_h=3)
        conflicts = await _check_conflicts(
            db,
            [DEVICE_A, DEVICE_B],
            NOW + timedelta(hours=1),
            NOW + timedelta(hours=3),
            exclusive_device_ids={str(DEVICE_A), str(DEVICE_B)},
        )
        assert sorted(str(d) for d in conflicts) == sorted([str(DEVICE_A), str(DEVICE_B)])


# --- reservation_devices join table / association proxy ---


@pytest.mark.asyncio
async def test_device_ids_proxy_construct_and_reassign():
    """device_ids round-trips through the join table; reassignment replaces the
    membership set via delete-orphan (no leftover rows)."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        count = (await db.execute(select(func.count()).select_from(ReservationDevice))).scalar()
        assert count == 2
        assert {str(d) for d in res.device_ids} == {str(DEVICE_A), str(DEVICE_B)}

        res.device_ids = [DEVICE_A]
        await db.commit()
        await db.refresh(res)
        count_after = (
            await db.execute(select(func.count()).select_from(ReservationDevice))
        ).scalar()
        assert count_after == 1
        assert [str(d) for d in res.device_ids] == [str(DEVICE_A)]


@pytest.mark.asyncio
async def test_device_ids_readable_after_session_close():
    """Regression for MissingGreenlet: selectin eager-loading + expire_on_commit
    False keeps device_ids readable after the loading session is committed and
    closed (the expiration task and response serialization depend on this)."""
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])

    async with TestSessionLocal() as db:
        loaded = (await db.execute(select(Reservation))).scalars().all()
        await db.commit()

    # Session is closed; reading the proxy must not trigger lazy IO.
    assert {str(d) for d in loaded[0].device_ids} == {str(DEVICE_A), str(DEVICE_B)}


# --- list_calendar_reservations device / visibility filters ---


@pytest.mark.asyncio
async def test_calendar_device_id_filter():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A])
        window = (NOW, NOW + timedelta(hours=10))
        assert await list_calendar_reservations(db, *window, device_id=DEVICE_B) == []
        match = await list_calendar_reservations(db, *window, device_id=DEVICE_A)
        assert len(match) == 1


@pytest.mark.asyncio
async def test_calendar_visibility_subset_and_empty_set():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        window = (NOW, NOW + timedelta(hours=10))
        # Not every device is visible -> excluded.
        assert (
            await list_calendar_reservations(db, *window, visible_device_ids={str(DEVICE_A)}) == []
        )
        # All devices visible -> included.
        all_visible = await list_calendar_reservations(
            db, *window, visible_device_ids={str(DEVICE_A), str(DEVICE_B)}
        )
        assert len(all_visible) == 1
        # Empty visible set -> nothing is all-visible.
        assert await list_calendar_reservations(db, *window, visible_device_ids=set()) == []


# --- _publish_nats_event ---


@pytest.mark.asyncio
async def test_publish_nats_event_none_connection():
    # Should return without error
    await _publish_nats_event(None, "herd.test", {"event": "test"})


@pytest.mark.asyncio
async def test_publish_nats_event_success():
    mock_js = AsyncMock()
    mock_nc = MagicMock()
    mock_nc.jetstream.return_value = mock_js
    await _publish_nats_event(mock_nc, "herd.test", {"event": "test"})
    mock_js.publish.assert_called_once()


@pytest.mark.asyncio
async def test_publish_nats_event_exception_logged():
    mock_js = AsyncMock()
    mock_js.publish.side_effect = Exception("NATS down")
    mock_nc = MagicMock()
    mock_nc.jetstream.return_value = mock_js
    # Should not raise
    await _publish_nats_event(mock_nc, "herd.test", {"event": "test"})


# --- _update_device_statuses ---


@pytest.mark.asyncio
async def test_update_device_statuses_no_token():
    with patch("app.services.reservation_service.settings") as mock_settings:
        mock_settings.internal_api_token = ""
        await _update_device_statuses([DEVICE_A], "RESERVED")
        # Should return immediately, no httpx calls


@pytest.mark.asyncio
async def test_update_device_statuses_success():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        await _update_device_statuses([DEVICE_A, DEVICE_B], "RESERVED")
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_update_device_statuses_failure_logged():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post.side_effect = Exception("Connection refused")

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        # Default behavior: best-effort, does not raise.
        await _update_device_statuses([DEVICE_A], "RESERVED")


@pytest.mark.asyncio
async def test_update_device_statuses_raises_when_requested():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post.side_effect = Exception("Connection refused")

    with patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(RuntimeError, match="inventory status update failed"):
            await _update_device_statuses([DEVICE_A], "RESERVED", raise_on_failure=True)


# --- _acquire_device_locks ---


@pytest.mark.asyncio
async def test_acquire_device_locks_sqlite_noop():
    async with TestSessionLocal() as db:
        # Should be no-op on SQLite (no pg_advisory_xact_lock)
        await _acquire_device_locks(db, [DEVICE_A, DEVICE_B])


# --- create_reservation ---


@pytest.mark.asyncio
async def test_create_reservation_success():
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            res = await create_reservation(db, data, USER_ID, "token", username="testuser")
        assert res.status == ReservationStatus.ACTIVE
        assert res.owner_name == "testuser"
        assert res.topology_type == TopologyType.PHYSICAL


@pytest.mark.asyncio
async def test_create_reservation_future_is_pending_and_defers_provisioning():
    """A booking more than the start-grace ahead is created PENDING with no
    immediate provisioning: no inventory flip, no fork, no reservation.created
    (issue #132). The expiration task provisions it at start_time."""
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=1),
            end_time=NOW + timedelta(hours=3),
        )
        update_mock = AsyncMock()
        fork_mock = AsyncMock()
        publish_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
            patch(
                "app.services.reservation_service._create_reservation_fork_best_effort",
                new=fork_mock,
            ),
            patch("app.services.reservation_service._publish_nats_event", new=publish_mock),
        ):
            res = await create_reservation(db, data, USER_ID, "token")
        assert res.status == ReservationStatus.PENDING
        update_mock.assert_not_called()
        fork_mock.assert_not_awaited()
        publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_reservation_future_skips_now_availability_check():
    """A future booking is gated by conflict detection, not current device status:
    a device that is RESERVED right now can still be scheduled for a later window
    (issue #132)."""
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A, status="RESERVED")]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=1),
            end_time=NOW + timedelta(hours=3),
        )
        with patch(
            "app.services.reservation_service._fetch_devices", new=AsyncMock(return_value=devices)
        ):
            res = await create_reservation(db, data, USER_ID, "token")
        assert res.status == ReservationStatus.PENDING


@pytest.mark.asyncio
async def test_create_reservation_inventory_unreachable():
    async with TestSessionLocal() as db:
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=1),
            end_time=NOW + timedelta(hours=3),
        )
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=RuntimeError("Connection refused")),
        ):
            with pytest.raises(RuntimeError, match="Failed to contact"):
                await create_reservation(db, data, USER_ID, "token")


@pytest.mark.asyncio
async def test_create_reservation_device_not_found():
    async with TestSessionLocal() as db:
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=1),
            end_time=NOW + timedelta(hours=3),
        )
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=ValueError("Device not found")),
        ):
            with pytest.raises(ValueError, match="not found"):
                await create_reservation(db, data, USER_ID, "token")


@pytest.mark.asyncio
async def test_create_reservation_mixed_topology():
    async with TestSessionLocal() as db:
        devices = [
            _make_device(DEVICE_A, topology="PHYSICAL"),
            _make_device(DEVICE_B, topology="CLOUD"),
        ]
        data = ReservationCreate(
            device_ids=[DEVICE_A, DEVICE_B],
            start_time=NOW + timedelta(hours=1),
            end_time=NOW + timedelta(hours=3),
        )
        with patch(
            "app.services.reservation_service._fetch_devices", new=AsyncMock(return_value=devices)
        ):
            with pytest.raises(ValueError, match="topology type"):
                await create_reservation(db, data, USER_ID, "token")


@pytest.mark.asyncio
async def test_create_reservation_device_unavailable():
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A, status="RESERVED")]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        with patch(
            "app.services.reservation_service._fetch_devices", new=AsyncMock(return_value=devices)
        ):
            with pytest.raises(ValueError, match="not available"):
                await create_reservation(db, data, USER_ID, "token")


@pytest.mark.asyncio
async def test_create_reservation_non_exclusive_reserved_ok():
    """Non-exclusive device with RESERVED status should be allowed."""
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A, status="RESERVED", exclusive=False)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            res = await create_reservation(db, data, USER_ID, "token")
        assert res.status == ReservationStatus.ACTIVE


@pytest.mark.asyncio
async def test_create_reservation_conflict():
    async with TestSessionLocal() as db:
        # Pre-existing reservation
        await _insert_reservation(db, start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=2),
            end_time=NOW + timedelta(hours=4),
        )
        with patch(
            "app.services.reservation_service._fetch_devices", new=AsyncMock(return_value=devices)
        ):
            with pytest.raises(LookupError, match="Time conflict"):
                await create_reservation(db, data, USER_ID, "token")


@pytest.mark.asyncio
async def test_create_reservation_nats_event_published():
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A)]
        mock_nats = MagicMock()
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch(
                "app.services.reservation_service._publish_nats_event", new=AsyncMock()
            ) as mock_pub,
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            await create_reservation(db, data, USER_ID, "token", nats_conn=mock_nats)
            mock_pub.assert_called_once()


@pytest.mark.asyncio
async def test_create_reservation_non_exclusive_skips_pending_provision():
    """Non-exclusive devices need no inventory flip, so status goes straight to ACTIVE."""
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A, exclusive=False)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        mock_update = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            patch("app.services.reservation_service._update_device_statuses", new=mock_update),
        ):
            res = await create_reservation(db, data, USER_ID, "token")
        assert res.status == ReservationStatus.ACTIVE
        mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_create_reservation_fails_when_inventory_exhausts_retries():
    """Retries exhausted -> reservation persists as FAILED, no NATS event, RuntimeError raised."""
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        mock_update = AsyncMock(side_effect=RuntimeError("inventory down"))
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch(
                "app.services.reservation_service._publish_nats_event", new=AsyncMock()
            ) as mock_pub,
            patch("app.services.reservation_service._update_device_statuses", new=mock_update),
            # Collapse backoff delays so the test runs fast.
            patch("herd_common.retry.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="Failed to reserve devices"):
                await create_reservation(db, data, USER_ID, "token")

        # _update_device_statuses was retried 3 times (default attempts).
        assert mock_update.await_count == 3
        # NATS event was never emitted on the failure path.
        mock_pub.assert_not_called()

        # Reservation row persists in FAILED state for audit.
        from sqlalchemy import select

        rows = (await db.execute(select(Reservation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == ReservationStatus.FAILED


@pytest.mark.asyncio
async def test_create_reservation_reverts_partially_reserved_devices_on_failure():
    """Regression: on a partial RESERVED failure that exhausts retries, the devices
    that DID get set RESERVED must be reverted to AVAILABLE so they are not orphaned
    (FAILED reservations are excluded from the conflict set, so nothing references
    them; without the compensating revert they stay RESERVED and unbookable).

    DEVICE_A's RESERVED POST always fails; DEVICE_B's succeeds. The create path
    should flip to FAILED, raise, and issue a compensating AVAILABLE POST for
    DEVICE_B (and not for DEVICE_A, which never reached RESERVED).
    """
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        data = ReservationCreate(
            device_ids=[DEVICE_A, DEVICE_B],
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )

        # Record every inventory status POST as (device_id_str, status).
        posts: list[tuple[str, str]] = []

        async def fake_post(url, json=None, headers=None, timeout=None):
            device_id = url.rstrip("/").split("/")[-2]
            status = json["status"]
            posts.append((device_id, status))
            resp = MagicMock()
            if device_id == str(DEVICE_A) and status == "RESERVED":
                # DEVICE_A never succeeds at reserving, which drives the failure.
                resp.raise_for_status = MagicMock(side_effect=Exception("inventory 500"))
            else:
                resp.raise_for_status = MagicMock(return_value=None)
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=fake_post)

        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client),
            # Collapse backoff delays so the test runs fast.
            patch("herd_common.retry.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="Failed to reserve devices"):
                await create_reservation(db, data, USER_ID, "token")

        rows = (await db.execute(select(Reservation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == ReservationStatus.FAILED

        # The succeeded device (DEVICE_B) was reverted to AVAILABLE.
        assert (str(DEVICE_B), "AVAILABLE") in posts
        # DEVICE_B was set RESERVED at least once (the success the revert compensates).
        assert (str(DEVICE_B), "RESERVED") in posts
        # DEVICE_A never reached RESERVED, so it must not be reverted.
        assert (str(DEVICE_A), "AVAILABLE") not in posts


@pytest.mark.asyncio
async def test_create_reservation_pending_provision_blocks_concurrent_create():
    """A PENDING_PROVISION row must conflict with a second create for the same device."""
    async with TestSessionLocal() as db:
        # Simulate a reservation currently mid-provisioning.
        await _insert_reservation(
            db,
            device_ids=[DEVICE_A],
            status=ReservationStatus.PENDING_PROVISION,
            start_offset_h=1,
            end_offset_h=3,
        )
        devices = [_make_device(DEVICE_A)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            start_time=NOW + timedelta(hours=2),
            end_time=NOW + timedelta(hours=4),
        )
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(LookupError, match="Time conflict"):
                await create_reservation(db, data, USER_ID, "token")


# --- list_user_reservations ---


@pytest.mark.asyncio
async def test_list_user_reservations_empty():
    async with TestSessionLocal() as db:
        items, total = await list_user_reservations(db, USER_ID)
        assert items == []
        assert total == 0


@pytest.mark.asyncio
async def test_list_user_reservations_pagination():
    async with TestSessionLocal() as db:
        for _ in range(3):
            await _insert_reservation(db)
        items, total = await list_user_reservations(db, USER_ID, skip=1, limit=1)
        assert len(items) == 1
        assert total == 3


# --- list_calendar_reservations ---


@pytest.mark.asyncio
async def test_list_calendar_all():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, start_offset_h=1, end_offset_h=3)
        results = await list_calendar_reservations(
            db,
            NOW,
            NOW + timedelta(hours=5),
        )
        assert len(results) == 1


@pytest.mark.asyncio
async def test_list_calendar_status_filter():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, status=ReservationStatus.ACTIVE)
        results = await list_calendar_reservations(
            db,
            NOW,
            NOW + timedelta(hours=5),
            status_filter=[ReservationStatus.PENDING],
        )
        assert results == []


@pytest.mark.asyncio
async def test_list_calendar_device_filter():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A])
        results = await list_calendar_reservations(
            db,
            NOW,
            NOW + timedelta(hours=5),
            device_id=DEVICE_B,
        )
        assert results == []


@pytest.mark.asyncio
async def test_list_calendar_visible_device_ids():
    async with TestSessionLocal() as db:
        await _insert_reservation(db, device_ids=[DEVICE_A])
        # DEVICE_A not in visible set
        results = await list_calendar_reservations(
            db,
            NOW,
            NOW + timedelta(hours=5),
            visible_device_ids={str(DEVICE_B)},
        )
        assert results == []
        # DEVICE_A in visible set
        results2 = await list_calendar_reservations(
            db,
            NOW,
            NOW + timedelta(hours=5),
            visible_device_ids={str(DEVICE_A)},
        )
        assert len(results2) == 1


# --- get_reservation ---


@pytest.mark.asyncio
async def test_get_reservation_found():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        found = await get_reservation(db, res.id, USER_ID)
        assert found is not None
        assert found.id == res.id


@pytest.mark.asyncio
async def test_get_reservation_wrong_user():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        found = await get_reservation(db, res.id, uuid.uuid4())
        assert found is None


# --- cancel_reservation ---


@pytest.mark.asyncio
async def test_cancel_reservation_success():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")
        assert cancelled.status == ReservationStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_reservation_not_found():
    async with TestSessionLocal() as db:
        result = await cancel_reservation(db, uuid.uuid4(), USER_ID, "token")
        assert result is None


@pytest.mark.asyncio
async def test_cancel_already_cancelled():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.CANCELLED)
        result = await cancel_reservation(db, res.id, USER_ID, "token")
        assert result.status == ReservationStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_completed():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.COMPLETED)
        result = await cancel_reservation(db, res.id, USER_ID, "token")
        assert result.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_failed_is_terminal_no_release_no_event():
    """Regression: cancelling a FAILED reservation must be a no-op.

    A FAILED reservation holds no devices (the provisioning failure reverted
    them), and those devices may since have been RESERVED by another user's
    ACTIVE reservation. The old guard let FAILED through, which (a) flipped the
    devices to AVAILABLE in inventory and (b) emitted reservation.cancelled,
    causing the execution consumer to disconnect L1 ports and tear down VLANs
    on wiring owned by the newer reservation.
    """
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.FAILED)
        fetch_mock = AsyncMock(return_value=[_make_device(DEVICE_A)])
        update_mock = AsyncMock()
        publish_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=fetch_mock,
            ),
            patch(
                "app.services.reservation_service._update_device_statuses",
                new=update_mock,
            ),
            patch("app.services.reservation_service._publish_nats_event", new=publish_mock),
        ):
            result = await cancel_reservation(db, res.id, USER_ID, "token")

    assert result is not None
    assert result.status == ReservationStatus.FAILED, "FAILED must stay FAILED for audit"
    update_mock.assert_not_awaited()
    publish_mock.assert_not_awaited()
    fetch_mock.assert_not_awaited()


# --- release_reservation ---


@pytest.mark.asyncio
async def test_release_reservation_success():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
        ):
            released = await release_reservation(db, res.id, USER_ID, "token")
        assert released.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_release_reservation_not_found():
    async with TestSessionLocal() as db:
        result = await release_reservation(db, uuid.uuid4(), USER_ID, "token")
        assert result is None


@pytest.mark.asyncio
async def test_release_non_active():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.PENDING)
        result = await release_reservation(db, res.id, USER_ID, "token")
        assert result.status == ReservationStatus.PENDING


# --- cancel/release reliability (Branch 1 fix) ---


@pytest.mark.asyncio
async def test_cancel_reservation_retries_and_logs_when_release_fails(caplog):
    """Repro for the state-leak bug: when inventory keeps failing the AVAILABLE
    write during cancel, we want a bounded retry, a structured failure log so
    ops can see it, and the reservation still flipped to CANCELLED (the booking
    is cancelled from the user's POV; the inventory leak is a separate problem
    a future sweeper will reconcile).
    """
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
            patch(
                "app.services.reservation_service._update_device_statuses",
                new=update_mock,
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            caplog.at_level("ERROR", logger="app.services.reservation_service"),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")

    assert cancelled.status == ReservationStatus.CANCELLED
    assert update_mock.await_count == 3, "expected retry_with_backoff(attempts=3)"
    release_failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_cancel_release_failed"
    ]
    assert release_failed, "expected structured log action=reservation_cancel_release_failed"
    assert str(res.id) in release_failed[0].getMessage() or getattr(
        release_failed[0], "reservation_id", None
    ) == str(res.id)


@pytest.mark.asyncio
async def test_cancel_reservation_release_succeeds_after_one_retry(caplog):
    """Transient inventory failure: first attempt fails, second succeeds. The
    reservation cancels normally and NO failure log is emitted.
    """
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        update_mock = AsyncMock(side_effect=[RuntimeError("inventory 503"), None])
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
            patch(
                "app.services.reservation_service._update_device_statuses",
                new=update_mock,
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            caplog.at_level("ERROR", logger="app.services.reservation_service"),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")

    assert cancelled.status == ReservationStatus.CANCELLED
    assert update_mock.await_count == 2
    release_failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_cancel_release_failed"
    ]
    assert not release_failed, "transient failure should not emit the failure log"


@pytest.mark.asyncio
async def test_release_reservation_retries_and_logs_when_inventory_fails(caplog):
    """Same shape as the cancel test, applied to release_reservation."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db)
        update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
            patch(
                "app.services.reservation_service._update_device_statuses",
                new=update_mock,
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            caplog.at_level("ERROR", logger="app.services.reservation_service"),
        ):
            released = await release_reservation(db, res.id, USER_ID, "token")

    assert released.status == ReservationStatus.COMPLETED
    assert update_mock.await_count == 3
    release_failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_release_inventory_failed"
    ]
    assert release_failed, "expected structured log action=reservation_release_inventory_failed"


@pytest.mark.asyncio
async def test_fetch_devices_best_effort_returns_mixed_results():
    """One device fetch failing should not cause the whole list to be treated as
    a hard error on the cancel/release path. The best-effort helper returns
    successes as dicts and per-device failures as exception entries so the
    caller can decide policy (cancel/release: assume exclusive for failed ones).
    The strict _fetch_devices stays as-is for create/update which want
    all-or-nothing semantics.
    """
    bad_id = uuid.uuid4()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def fake_get(url, **_):
        resp = MagicMock()
        if str(bad_id) in url:
            resp.status_code = 503
            resp.raise_for_status = MagicMock(side_effect=Exception("503"))
        else:
            resp.status_code = 200
            resp.raise_for_status = MagicMock(return_value=None)
            # URL is /devices/<uuid>/internal; the uuid is the second-to-last segment.
            device_id = url.rstrip("/").rsplit("/", 2)[-2]
            resp.json = MagicMock(return_value=_make_device(device_id))
        return resp

    mock_client.get = fake_get
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "test-internal-token"
        mock_settings.inventory_service_url = "http://inv"
        results = await _fetch_devices_best_effort([DEVICE_A, bad_id, DEVICE_B])

    # Successful fetches are returned, the failing one is an exception entry.
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 2
    assert len(failures) == 1
    assert {d["id"] for d in successes} == {str(DEVICE_A), str(DEVICE_B)}


# --- expiration ---


@pytest.mark.asyncio
async def test_expiration_cycle_activates_pending():
    from app.tasks.expiration import _run_expiration_cycle

    async with TestSessionLocal() as db:
        res = await _insert_reservation(
            db,
            status=ReservationStatus.PENDING,
            start_offset_h=-2,
            end_offset_h=1,
        )
        res_id = res.id

    with (
        patch("app.tasks.expiration.AsyncSessionLocal", TestSessionLocal),
        patch("app.tasks.expiration._update_device_statuses", new=AsyncMock()),
        patch(
            "app.tasks.expiration._fetch_devices_best_effort",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await _run_expiration_cycle()

    async with TestSessionLocal() as db:
        from sqlalchemy import select

        result = await db.execute(select(Reservation).where(Reservation.id == res_id))
        updated = result.scalar_one()
        assert updated.status == ReservationStatus.ACTIVE


@pytest.mark.asyncio
async def test_expiration_cycle_completes_expired():
    from app.tasks.expiration import _run_expiration_cycle

    async with TestSessionLocal() as db:
        res = await _insert_reservation(
            db,
            status=ReservationStatus.ACTIVE,
            start_offset_h=-3,
            end_offset_h=-1,
        )
        res_id = res.id

    mock_update = AsyncMock()
    with (
        patch("app.tasks.expiration.AsyncSessionLocal", TestSessionLocal),
        patch("app.tasks.expiration._update_device_statuses", new=mock_update),
        patch(
            "app.tasks.expiration._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
        ),
    ):
        await _run_expiration_cycle()
        mock_update.assert_called_once()

    async with TestSessionLocal() as db:
        from sqlalchemy import select

        result = await db.execute(select(Reservation).where(Reservation.id == res_id))
        updated = result.scalar_one()
        assert updated.status == ReservationStatus.COMPLETED


# --- update_reservation service-level tests ---


@pytest.mark.asyncio
async def test_update_reservation_no_changes():
    """Empty update body (no fields set) should succeed without changes."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        original_end = res.end_time
        original_purpose = res.purpose

        with patch(
            "app.services.reservation_service._publish_nats_event",
            new=AsyncMock(),
        ):
            result = await update_reservation(db, res.id, USER_ID, ReservationUpdate())
        assert result is not None
        assert result.end_time == original_end
        assert result.purpose == original_purpose


@pytest.mark.asyncio
async def test_update_reservation_cancelled_rejected():
    """Cannot update a CANCELLED reservation."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.CANCELLED)

        with pytest.raises(ValueError, match="CANCELLED"):
            await update_reservation(db, res.id, USER_ID, ReservationUpdate(purpose="nope"))


@pytest.mark.asyncio
async def test_update_reservation_failed_rejected():
    """Cannot update a FAILED reservation."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.FAILED)

        with pytest.raises(ValueError, match="FAILED"):
            await update_reservation(db, res.id, USER_ID, ReservationUpdate(purpose="nope"))


@pytest.mark.asyncio
async def test_update_reservation_pending_provision_rejected():
    """Cannot update a PENDING_PROVISION reservation (avoids mid-provisioning changes)."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, status=ReservationStatus.PENDING_PROVISION)

        with pytest.raises(ValueError, match="PENDING_PROVISION"):
            await update_reservation(db, res.id, USER_ID, ReservationUpdate(purpose="nope"))


@pytest.mark.asyncio
async def test_update_reservation_extend_non_exclusive_skips_conflict():
    """Non-exclusive devices are not checked for conflicts during extension."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])

        new_end = NOW + timedelta(hours=5)
        non_excl_device = _make_device(DEVICE_A, exclusive=False)

        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[non_excl_device]),
            ),
            patch(
                "app.services.reservation_service._publish_nats_event",
                new=AsyncMock(),
            ),
        ):
            result = await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=new_end),
                token="fake",
            )
        assert result is not None


@pytest.mark.asyncio
async def test_update_reservation_remove_non_exclusive_no_status_change():
    """Removing a non-exclusive device should not try to set it AVAILABLE."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])

        non_excl_a = _make_device(DEVICE_A, exclusive=False)
        mock_fetch = AsyncMock(
            side_effect=[
                [_make_device(DEVICE_A)],  # fetch for new device set
                [non_excl_a],  # fetch for removed device check
            ]
        )
        mock_update_statuses = AsyncMock()

        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=mock_fetch,
            ),
            patch(
                "app.services.reservation_service._publish_nats_event",
                new=AsyncMock(),
            ),
            patch(
                "app.services.reservation_service._update_device_statuses",
                new=mock_update_statuses,
            ),
        ):
            result = await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A]),
                token="fake",
            )
        assert result is not None
        # No AVAILABLE call since removed device is non-exclusive
        available_calls = [c for c in mock_update_statuses.call_args_list if c[0][1] == "AVAILABLE"]
        assert len(available_calls) == 0


# --- reservation.updated payload honesty (issue #79) ---


@pytest.mark.asyncio
async def test_update_reservation_metadata_only_omits_end_time():
    """A purpose-only edit must not advertise an unchanged end time.

    The published reservation.updated payload should flag end_time_changed=False
    and carry a null end_time so the notifications consumer suppresses the
    misleading "ends <time>" notification.
    """
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        original_end = res.end_time

        mock_publish = AsyncMock()
        with patch(
            "app.services.reservation_service._publish_nats_event",
            new=mock_publish,
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(purpose="new purpose")
            )

        assert result is not None
        assert result.end_time == original_end
        payload = mock_publish.call_args.args[2]
        assert payload["event"] == "reservation.updated"
        assert payload["end_time_changed"] is False
        assert payload["end_time"] is None
        assert payload["added_device_ids"] == []
        assert payload["removed_device_ids"] == []


@pytest.mark.asyncio
async def test_update_reservation_same_end_time_not_flagged_changed():
    """Re-sending the existing end_time is not a change; flag stays False."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        same_end = res.end_time

        mock_publish = AsyncMock()
        with patch(
            "app.services.reservation_service._publish_nats_event",
            new=mock_publish,
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(end_time=same_end)
            )

        assert result is not None
        payload = mock_publish.call_args.args[2]
        assert payload["end_time_changed"] is False
        assert payload["end_time"] is None


@pytest.mark.asyncio
async def test_update_reservation_changed_end_time_is_flagged_and_carried():
    """Extending the reservation flags the change and carries the new end_time."""
    from app.services.reservation_service import update_reservation

    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        new_end = NOW + timedelta(hours=6)
        non_excl_device = _make_device(DEVICE_A, exclusive=False)

        mock_publish = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[non_excl_device]),
            ),
            patch(
                "app.services.reservation_service._publish_nats_event",
                new=mock_publish,
            ),
        ):
            result = await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=new_end),
                token="fake",
            )

        assert result is not None
        payload = mock_publish.call_args.args[2]
        assert payload["end_time_changed"] is True
        assert payload["end_time"] == result.end_time.isoformat()


# --- fork-on-activation (issue #25) ---


@pytest.mark.asyncio
async def test_create_reservation_fork_skips_when_no_topology():
    """No parent topology means no fork at activation (Decision 3 Case A skip).

    The cabling client must not even be constructed.
    """
    with patch("app.services.reservation_service.httpx.AsyncClient") as mock_client:
        await _create_reservation_fork(uuid.uuid4(), None)
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_create_reservation_fork_posts_to_cabling():
    """With a topology, we POST /internal/forks with parent_version_id null so
    cabling pins the parent's current version itself (Decision 3 Case B)."""
    reservation_id = uuid.uuid4()
    topology_id = uuid.uuid4()

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.reservation_service.settings") as mock_settings,
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cabling:8000"
        await _create_reservation_fork(reservation_id, topology_id)

    mock_client.post.assert_awaited_once()
    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["reservation_id"] == str(reservation_id)
    assert kwargs["json"]["parent_topology_id"] == str(topology_id)
    assert kwargs["json"]["parent_version_id"] is None
    assert kwargs["headers"]["X-Internal-Token"] == "tok"


@pytest.mark.asyncio
async def test_create_reservation_fork_no_token_skips():
    with patch("app.services.reservation_service.settings") as mock_settings:
        mock_settings.internal_api_token = ""
        # Should return without raising and without an HTTP client.
        with patch("app.services.reservation_service.httpx.AsyncClient") as mock_client:
            await _create_reservation_fork(uuid.uuid4(), uuid.uuid4())
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_create_reservation_fork_raises_on_4xx():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "boom"
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.reservation_service.settings") as mock_settings,
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cabling:8000"
        with pytest.raises(RuntimeError, match="500"):
            await _create_reservation_fork(uuid.uuid4(), uuid.uuid4())


@pytest.mark.asyncio
async def test_fork_best_effort_swallows_exhausted_retries():
    """A fork-create that fails every retry is logged and swallowed: it must NOT
    raise out and strand the provisioned reservation."""
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(side_effect=RuntimeError("cabling down")),
    ):
        # No exception escapes.
        await _create_reservation_fork_best_effort(uuid.uuid4(), uuid.uuid4())


@pytest.mark.asyncio
async def test_fork_best_effort_skips_no_topology():
    """No topology means the best-effort wrapper short-circuits without retrying."""
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(),
    ) as mock_fork:
        await _create_reservation_fork_best_effort(uuid.uuid4(), None)
    mock_fork.assert_not_called()


@pytest.mark.asyncio
async def test_create_reservation_invokes_fork_when_topology_present():
    """End-to-end: an ACTIVE reservation with a topology fires fork creation once."""
    topology_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        devices = [_make_device(DEVICE_A, exclusive=False)]
        data = ReservationCreate(
            device_ids=[DEVICE_A],
            topology_id=topology_id,
            start_time=NOW,
            end_time=NOW + timedelta(hours=3),
        )
        fork_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch(
                "app.services.reservation_service._validate_topology_connectivity",
                new=AsyncMock(),
            ),
            patch("app.services.reservation_service._publish_nats_event", new=AsyncMock()),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
            patch(
                "app.services.reservation_service._create_reservation_fork_best_effort",
                new=fork_mock,
            ),
        ):
            res = await create_reservation(db, data, USER_ID, "token")
        assert res.status == ReservationStatus.ACTIVE
        fork_mock.assert_awaited_once()
        called_args = fork_mock.call_args.args
        assert called_args[0] == res.id
        assert called_args[1] == topology_id
