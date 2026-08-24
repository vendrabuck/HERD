"""Targeted unit tests closing service-layer, schema, expiration, and reporting
coverage gaps.

These exercise branches the existing suites skip: the internal-token device
fetch helpers (no-token and 404 paths), topology connectivity validation, the
advisory-lock Postgres branch, the update-reservation extend/add/remove device
paths, the cancel/release exclusive-device selection, the expiration_loop
exception handling, and a few small schema/reporting validators. All external
HTTP is mocked; NATS publishing is stubbed; no real DB or network is used.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base
from app.models.reservation import (
    Reservation,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationUpdate, _as_utc
from app.services.reservation_service import (
    _acquire_device_locks,
    _fetch_devices_best_effort,
    _release_exclusive_devices_best_effort,
    _validate_topology_connectivity,
    cancel_reservation,
    release_reservation,
    update_reservation,
)
from sqlalchemy import select
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
    topology_id=None,
):
    res = Reservation(
        user_id=user_id or USER_ID,
        device_ids=[str(d) for d in (device_ids or [DEVICE_A])],
        topology_id=topology_id,
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


def _mock_httpx_client(get_resp=None, post_resp=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get_resp is not None:
        client.get = AsyncMock(return_value=get_resp)
    if post_resp is not None:
        client.post = AsyncMock(return_value=post_resp)
    return client


# --- _fetch_devices_best_effort: no-token and 404 branches ---


@pytest.mark.asyncio
async def test_fetch_devices_best_effort_no_token_returns_runtime_errors():
    """Without an internal token the helper returns one RuntimeError per device
    so callers fall back to assuming every device is exclusive."""
    with patch("app.services.reservation_service.settings") as mock_settings:
        mock_settings.internal_api_token = ""
        results = await _fetch_devices_best_effort([DEVICE_A, DEVICE_B])
    assert len(results) == 2
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_fetch_devices_best_effort_404_is_value_error_entry():
    """A 404 on one device surfaces as a per-device ValueError entry (not a raise)."""
    resp = MagicMock()
    resp.status_code = 404
    client = _mock_httpx_client(get_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.inventory_service_url = "http://inv"
        results = await _fetch_devices_best_effort([DEVICE_A])
    assert len(results) == 1
    assert isinstance(results[0], ValueError)


# --- _validate_topology_connectivity ---


@pytest.mark.asyncio
async def test_validate_topology_connectivity_valid():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": True}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_topology_connectivity_404_is_noop():
    """Topology deleted between selection and submit returns 404, treated as no
    validation rather than a hard failure."""
    resp = MagicMock()
    resp.status_code = 404
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_topology_connectivity_server_error_raises_runtime():
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "boom"
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(RuntimeError, match="Cabling validation returned 500"):
            await _validate_topology_connectivity(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_topology_connectivity_transport_error_raises_runtime():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(RuntimeError, match="Failed to contact cabling service"):
            await _validate_topology_connectivity(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_edges_raises_value_error():
    """Unreachable edges are summarized (first five) with an "and N more" suffix."""
    invalid = [{"edge_id": f"e{i}", "reason": "no path"} for i in range(7)]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": False, "invalid_edges": invalid}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError, match="unreachable edges") as excinfo:
            await _validate_topology_connectivity(uuid.uuid4())
    # 7 invalid edges -> first 5 listed, "and 2 more" appended.
    assert "and 2 more" in str(excinfo.value)
    assert "e0 (no path)" in str(excinfo.value)


# --- _acquire_device_locks postgresql branch ---


@pytest.mark.asyncio
async def test_acquire_device_locks_postgres_issues_advisory_locks():
    """On Postgres the helper issues one pg_advisory_xact_lock per device, in
    sorted order to avoid deadlocks."""
    db = MagicMock()
    db.bind = MagicMock()
    db.bind.dialect.name = "postgresql"
    db.execute = AsyncMock()
    await _acquire_device_locks(db, [DEVICE_A, DEVICE_B])
    assert db.execute.await_count == 2


# --- update_reservation gaps ---


@pytest.mark.asyncio
async def test_update_reservation_not_found_returns_none():
    async with TestSessionLocal() as db:
        result = await update_reservation(db, uuid.uuid4(), USER_ID, ReservationUpdate(purpose="x"))
        assert result is None


@pytest.mark.asyncio
async def test_update_reservation_end_time_not_after_start_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, start_offset_h=2, end_offset_h=4)
        # New end before the reservation start.
        with pytest.raises(ValueError, match="after start_time"):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=NOW + timedelta(hours=1)),
            )


@pytest.mark.asyncio
async def test_update_reservation_end_time_in_past_rejected():
    """A new end_time after start but already in the past is rejected."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, start_offset_h=-5, end_offset_h=5)
        with pytest.raises(ValueError, match="in the future"):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=NOW - timedelta(hours=1)),
            )


@pytest.mark.asyncio
async def test_update_reservation_extend_fetch_failure_falls_back_to_exclusive():
    """If the device fetch during an extend fails, every device is treated as
    exclusive and the conflict check still runs (here: no conflict, so the
    extend succeeds)."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], end_offset_h=3)
        new_end = NOW + timedelta(hours=5)
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(side_effect=RuntimeError("inventory down")),
            ),
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(end_time=new_end), token="t"
            )
        assert result is not None
        assert result.end_time.replace(tzinfo=None) == new_end.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_update_reservation_extend_conflict_rejected():
    """Extending into a window already held by another exclusive reservation on
    the same device raises LookupError."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        # A conflicting reservation occupying hours 4-6 on the same device.
        await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=4, end_offset_h=6)
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
        ):
            with pytest.raises(LookupError, match="extended window"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(end_time=NOW + timedelta(hours=5)),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_purpose_only():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        result = await update_reservation(
            db, res.id, USER_ID, ReservationUpdate(purpose="new purpose")
        )
        assert result.purpose == "new purpose"


@pytest.mark.asyncio
async def test_update_reservation_add_device_fetch_failure_raises_runtime():
    """A non-ValueError fetch failure while validating an added device surfaces
    as RuntimeError (mapped to 503 upstream)."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=RuntimeError("inventory down")),
        ):
            with pytest.raises(RuntimeError, match="Failed to contact inventory"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_fetch_value_error_propagates():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=ValueError("Device not found")),
        ):
            with pytest.raises(ValueError, match="not found"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_mixed_topology_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        devices = [
            _make_device(DEVICE_A, topology="PHYSICAL"),
            _make_device(DEVICE_B, topology="CLOUD"),
        ]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(ValueError, match="topology type"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_unavailable_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        devices = [
            _make_device(DEVICE_A),
            _make_device(DEVICE_B, status="RESERVED"),
        ]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(ValueError, match="not available"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_conflict_rejected():
    """Adding an exclusive device that is already reserved in the overlapping
    window raises LookupError."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        # DEVICE_B is held by another reservation overlapping the same window.
        await _insert_reservation(db, device_ids=[DEVICE_B], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(LookupError, match="already reserved"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_exclusive_device_marks_reserved():
    """Successfully adding an exclusive device flips it to RESERVED in inventory
    and persists the new membership."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        assert {str(d) for d in result.device_ids} == {str(DEVICE_A), str(DEVICE_B)}
        reserved_calls = [c for c in update_mock.call_args_list if c[0][1] == "RESERVED"]
        assert reserved_calls, "added exclusive device should be marked RESERVED"


@pytest.mark.asyncio
async def test_update_reservation_remove_exclusive_device_marks_available():
    """Removing an exclusive device flips it back to AVAILABLE."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        mock_fetch = AsyncMock(
            side_effect=[
                [_make_device(DEVICE_A)],  # new device set validation
                [_make_device(DEVICE_B)],  # removed device fetch (exclusive)
            ]
        )
        update_mock = AsyncMock()
        with (
            patch("app.services.reservation_service._fetch_devices", new=mock_fetch),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=[DEVICE_A]), token="t"
            )
        assert [str(d) for d in result.device_ids] == [str(DEVICE_A)]
        available_calls = [c for c in update_mock.call_args_list if c[0][1] == "AVAILABLE"]
        assert available_calls, "removed exclusive device should be marked AVAILABLE"


@pytest.mark.asyncio
async def test_update_reservation_remove_device_fetch_failure_assumes_exclusive():
    """If the removed-device fetch fails, the device is assumed exclusive and
    released to AVAILABLE anyway."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        mock_fetch = AsyncMock(
            side_effect=[
                [_make_device(DEVICE_A)],  # new device set validation
                RuntimeError("inventory down"),  # removed device fetch fails
            ]
        )
        update_mock = AsyncMock()
        with (
            patch("app.services.reservation_service._fetch_devices", new=mock_fetch),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=[DEVICE_A]), token="t"
            )
        assert [str(d) for d in result.device_ids] == [str(DEVICE_A)]
        available_calls = [c for c in update_mock.call_args_list if c[0][1] == "AVAILABLE"]
        assert available_calls, "fetch failure should still release the removed device"


@pytest.mark.asyncio
async def test_update_reservation_device_change_revalidates_topology():
    """A device-set change on a reservation that references a topology
    re-validates connectivity before any inventory mutation."""
    async with TestSessionLocal() as db:
        topo = uuid.uuid4()
        res = await _insert_reservation(db, device_ids=[DEVICE_A], topology_id=topo)
        validate_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A), _make_device(DEVICE_B)]),
            ),
            patch(
                "app.services.reservation_service._validate_topology_connectivity",
                new=validate_mock,
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        validate_mock.assert_awaited_once_with(topo)


# --- cancel/release exclusive-device selection via fetched dict ---


def _fetch_aligned(device_ids, exclusive_id):
    """Build best-effort fetch results aligned to the reservation's device order,
    marking only `exclusive_id` exclusive. The service zips this list positionally
    against list(reservation.device_ids), so it must follow the same order."""
    return [_make_device(d, exclusive=(uuid.UUID(str(d)) == exclusive_id)) for d in device_ids]


@pytest.mark.asyncio
async def test_cancel_reservation_selects_exclusive_from_fetch_result():
    """When best-effort fetch returns a dict with exclusive=True, that device is
    released; a non-exclusive device is left alone."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        order = [uuid.UUID(str(d)) for d in res.device_ids]
        fetch_results = _fetch_aligned(order, DEVICE_A)
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=fetch_results),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")
        assert cancelled.status == ReservationStatus.CANCELLED
        # Only DEVICE_A (exclusive) is released.
        assert update_mock.await_count == 1
        released_ids = update_mock.call_args[0][0]
        assert released_ids == [DEVICE_A]


@pytest.mark.asyncio
async def test_release_reservation_selects_exclusive_from_fetch_result():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        order = [uuid.UUID(str(d)) for d in res.device_ids]
        fetch_results = _fetch_aligned(order, DEVICE_B)
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=fetch_results),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            released = await release_reservation(db, res.id, USER_ID, "token")
        assert released.status == ReservationStatus.COMPLETED
        assert update_mock.await_count == 1
        assert update_mock.call_args[0][0] == [DEVICE_B]


@pytest.mark.asyncio
async def test_cancel_reservation_all_non_exclusive_skips_inventory():
    """Every device non-exclusive: no inventory release call is made."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=False)]),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")
        assert cancelled.status == ReservationStatus.CANCELLED
        update_mock.assert_not_called()


# --- _release_exclusive_devices_best_effort retry-exhaustion parity (issue #571 item 4) --
#
# The exclusivity filtering itself is exercised above (via cancel/release); this pins
# the retry-exhaustion logging branch, which the sibling cancel/release paths pin at
# test_reservation_service_unit.py:1194 and :1257 (await_count == 3 plus a structured
# log). _release_exclusive_devices_best_effort is the shared helper both the
# provision-result-failed path (reservation_service.py, log_action=
# "reservation_provision_failed_release") and the expiration sweep's timeout backstop
# (expiration.py, log_action="provision_timeout_release") call; it swallows after
# retries rather than raising, so the caller's already-committed FAILED transition is
# never revisited.


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_retries_and_swallows_on_exhaustion(
    caplog,
):
    """A persistently failing inventory release retries 3 times, swallows the
    final exception, and emits the structured failure log the caller passes in
    as log_action."""
    reservation_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id, [DEVICE_A], "reservation_provision_failed_release"
        )

    assert update_mock.await_count == 3, "expected retry_with_backoff(attempts=3)"
    failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_provision_failed_release"
    ]
    assert failed, "expected structured log action=reservation_provision_failed_release"
    assert str(reservation_id) in failed[0].getMessage() or getattr(
        failed[0], "reservation_id", None
    ) == str(reservation_id)


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_honors_caller_log_action(caplog):
    """The log_action string is caller-supplied, not hardcoded: the expiration
    sweep's timeout backstop passes "provision_timeout_release" through the same
    helper, and that action name (not the provision-result sibling's) is what
    lands in the structured log."""
    reservation_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id, [DEVICE_A], "provision_timeout_release"
        )

    assert update_mock.await_count == 3
    actions = {getattr(rec, "action", None) for rec in caplog.records}
    assert "provision_timeout_release" in actions
    assert "reservation_provision_failed_release" not in actions


# --- expiration_loop one-iteration exception handling ---


class _StopLoop(Exception):
    pass


@pytest.mark.asyncio
async def test_expiration_loop_runs_both_cycles_and_handles_errors(caplog):
    """One loop iteration runs the expiration cycle and the reminder cycle, logs
    an error from each when they raise, then sleeps. We break out of the
    otherwise-infinite loop by having the post-iteration sleep raise."""
    from app.tasks import expiration

    expiration_cycle = AsyncMock(side_effect=RuntimeError("cycle boom"))
    reminder_cycle = AsyncMock(side_effect=RuntimeError("reminder boom"))

    with (
        patch.object(expiration, "_run_expiration_cycle", new=expiration_cycle),
        patch.object(expiration, "_run_reminder_cycle", new=reminder_cycle),
        patch.object(expiration.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop())),
        caplog.at_level("ERROR", logger="app.tasks.expiration"),
    ):
        with pytest.raises(_StopLoop):
            await expiration.expiration_loop(interval_seconds=1)

    expiration_cycle.assert_awaited_once()
    reminder_cycle.assert_awaited_once()
    messages = [r.getMessage() for r in caplog.records]
    assert any("Expiration cycle failed" in m for m in messages)
    assert any("Expiry reminder cycle failed" in m for m in messages)


@pytest.mark.asyncio
async def test_expiration_loop_happy_path_single_iteration():
    """Both cycles succeed; the loop still breaks on the patched sleep."""
    from app.tasks import expiration

    expiration_cycle = AsyncMock()
    reminder_cycle = AsyncMock()
    with (
        patch.object(expiration, "_run_expiration_cycle", new=expiration_cycle),
        patch.object(expiration, "_run_reminder_cycle", new=reminder_cycle),
        patch.object(expiration.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop())),
    ):
        with pytest.raises(_StopLoop):
            await expiration.expiration_loop(interval_seconds=5)

    expiration_cycle.assert_awaited_once()
    reminder_cycle.assert_awaited_once()


# --- schema validators ---


def test_as_utc_naive_treated_as_utc():
    naive = datetime(2026, 1, 1, 0, 0)
    out = _as_utc(naive)
    assert out.tzinfo == timezone.utc


def test_as_utc_aware_left_unchanged():
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert _as_utc(aware) is aware


def test_reservation_update_device_ids_none_passes():
    """Explicit device_ids=None is a valid no-op update; the validator's
    None-guard branch returns None rather than rejecting an empty list."""
    upd = ReservationUpdate(purpose="x", device_ids=None)
    assert upd.device_ids is None


def test_reservation_update_device_ids_dedupes():
    upd = ReservationUpdate(device_ids=[DEVICE_A, DEVICE_A, DEVICE_B])
    assert upd.device_ids == [DEVICE_A, DEVICE_B]


def test_reservation_response_coerces_string_device_ids():
    """ReservationResponse.coerce_device_ids accepts string ids and returns UUIDs."""
    from app.schemas.reservation import ReservationResponse

    now = datetime.now(timezone.utc)
    resp = ReservationResponse(
        id=uuid.uuid4(),
        user_id=USER_ID,
        device_ids=[str(DEVICE_A), str(DEVICE_B)],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        purpose=None,
        start_time=now,
        end_time=now + timedelta(hours=1),
        status=ReservationStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert resp.device_ids == [DEVICE_A, DEVICE_B]
    assert all(isinstance(d, uuid.UUID) for d in resp.device_ids)


# --- guard: device membership rows actually persisted on add (sanity) ---


@pytest.mark.asyncio
async def test_update_reservation_add_persists_membership_rows(caplog):
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        rows = (await db.execute(select(Reservation).where(Reservation.id == res.id))).scalar_one()
        assert {str(d) for d in rows.device_ids} == {str(DEVICE_A), str(DEVICE_B)}


# --- reporting_service residual gaps ---


@pytest.mark.asyncio
async def test_build_report_zero_duration_inside_window_is_skipped():
    """A zero-duration reservation strictly inside the window clips to zero hours
    and is skipped (the hours<=0 continue), so it contributes no buckets."""
    from app.services.reporting_service import build_utilization_report

    async with TestSessionLocal() as db:
        instant = NOW + timedelta(hours=1)
        res = Reservation(
            user_id=USER_ID,
            device_ids=[str(DEVICE_A)],
            topology_type=TopologyType.PHYSICAL,
            purpose="zero",
            start_time=instant,
            end_time=instant,  # zero-width window, strictly inside the query window
            status=ReservationStatus.COMPLETED,
        )
        db.add(res)
        await db.commit()

        report = await build_utilization_report(
            db, NOW, NOW + timedelta(hours=5), [ReservationStatus.COMPLETED]
        )
    assert report.total_reservations == 0
    assert report.by_user == []
    assert report.total_hours == 0.0


@pytest.mark.asyncio
async def test_build_report_skips_malformed_device_id_defensively():
    """The aggregation guards against a non-UUID device id (defense in depth even
    though the typed join table normally rejects it at write time). We feed a
    fake reservation carrying a bad id through a stubbed query result and assert
    user/topology hours still accrue while the bad device id is skipped."""
    from app.services import reporting_service

    fake = MagicMock()
    fake.user_id = USER_ID
    fake.owner_name = "alice"
    fake.topology_type = TopologyType.PHYSICAL
    fake.start_time = NOW + timedelta(hours=1)
    fake.end_time = NOW + timedelta(hours=2)
    fake.device_ids = ["not-a-uuid", str(DEVICE_A)]

    scalars = MagicMock()
    scalars.all.return_value = [fake]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=exec_result)

    report = await reporting_service.build_utilization_report(
        db, NOW, NOW + timedelta(hours=5), None
    )
    # The reservation still counts; only the malformed device id is dropped.
    assert report.total_reservations == 1
    assert len(report.by_device) == 1
    assert str(report.by_device[0].device_id) == str(DEVICE_A)


@pytest.mark.asyncio
async def test_fetch_user_groups_map_outer_exception_is_swallowed(caplog):
    """If the AsyncClient context manager itself blows up (not a per-user
    HTTPError), the outer guard logs and returns empty groups for every user
    rather than failing the whole report."""
    from app.services.reporting_service import fetch_user_groups_map

    with (
        patch("app.services.reporting_service.httpx.AsyncClient", side_effect=RuntimeError("boom")),
        caplog.at_level("WARNING", logger="app.services.reporting_service"),
    ):
        result = await fetch_user_groups_map([USER_ID, DEVICE_B], "Bearer tok")
    assert result == {USER_ID: [], DEVICE_B: []}
    assert any("unexpected error" in r.getMessage() for r in caplog.records)


# --- router residual gaps (csv ValueError handler, visible-devices non-200) ---


@pytest.fixture
async def admin_router_client():
    """Admin-authenticated ASGI client bound to this module's in-memory DB."""
    from app.database import get_db
    from app.dependencies.auth import get_current_user_payload, require_admin
    from app.main import app
    from app.routers.reservations import bearer_scheme
    from fastapi.security import HTTPAuthorizationCredentials
    from httpx import ASGITransport, AsyncClient

    async def _get_db():
        async with TestSessionLocal() as session:
            yield session

    admin_payload = {"sub": str(USER_ID), "username": "adminuser", "role": "admin"}
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    app.dependency_overrides[require_admin] = lambda: admin_payload
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="fake-token"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_csv_report_inverted_window_returns_422(admin_router_client):
    """build_utilization_report raises ValueError on an inverted window; the csv
    route maps it to 422 (covers the csv handler's except branch)."""
    start = (NOW).isoformat()
    end = (NOW - timedelta(hours=1)).isoformat()  # end before start
    resp = await admin_router_client.get(
        "/reports/utilization.csv",
        params={"start": start, "end": end, "section": "user"},
    )
    assert resp.status_code == 422


@pytest.fixture
async def non_admin_router_client():
    from app.database import get_db
    from app.dependencies.auth import get_current_user_payload
    from app.main import app
    from app.routers.reservations import bearer_scheme
    from fastapi.security import HTTPAuthorizationCredentials
    from httpx import ASGITransport, AsyncClient

    async def _get_db():
        async with TestSessionLocal() as session:
            yield session

    payload = {"sub": str(USER_ID), "username": "regular", "role": "user"}
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="fake-token"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_visible_devices_non_200_logs_and_fails_open(non_admin_router_client, caplog):
    """A non-200 (but non-exception) response from inventory's visible-devices
    endpoint logs a warning and returns None, so the visibility filter is
    skipped (fail-open) and the create proceeds to device validation."""
    inv_resp = MagicMock()
    inv_resp.status_code = 500
    inv_client = _mock_httpx_client(get_resp=inv_resp)

    with (
        # _fetch_visible_device_ids runs its real body against this stubbed httpx.
        patch("app.routers.reservations.httpx.AsyncClient", return_value=inv_client),
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        caplog.at_level("WARNING", logger="app.routers.reservations"),
    ):
        resp = await non_admin_router_client.post(
            "/",
            json={
                "device_ids": [str(DEVICE_A)],
                "purpose": "test",
                "start_time": (NOW + timedelta(hours=1)).isoformat(),
                "end_time": (NOW + timedelta(hours=3)).isoformat(),
            },
        )
    # Fail-open: visibility None means no 403; the create succeeds.
    assert resp.status_code == 201
    assert any("visible-devices returned 500" in r.getMessage() for r in caplog.records)
