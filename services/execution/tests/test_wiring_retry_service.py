"""Tests for wiring_retry_service: manual retry and the background auto-retry channel.

ADR 0007 Decision 6 items 2-3 (issue #345 P3b phase 4). Drives reattempt_reservation
and run_wiring_retry_tick through the same in-memory SQLite + mocked-sandbox harness the
phase-3 consumer tests use, and covers: a FAILED build flipped ACTIVE on success, the
non-retryable pinned-reason classification (no driver call), the frozen refusal, attempts
accumulation and last_error update on a repeat failure, the background batch cap, the
max-attempts cutoff, the loop backoff schedule, and the poller-only enable split.
"""

import asyncio
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.services.nats_consumer import (
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_UNRESOLVABLE_REASON,
)
from app.services.wiring_retry_service import (
    WiringReservationFrozen,
    is_retryable_failure,
    reattempt_reservation,
    run_wiring_retry_loop,
    run_wiring_retry_tick,
    start_wiring_retry_scheduler,
    stop_wiring_retry_scheduler,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
OTHER_RES_ID = str(uuid.uuid4())

SWITCH_DATA = {
    "id": SWITCH_ID,
    "name": "L1-Switch",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "L1 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _sandbox_recorder(fail_pairs=None):
    fail_pairs = fail_pairs or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        pa, pb = mk.get("port_a"), mk.get("port_b")
        calls.append((action, pa, pb))
        if pa is not None and frozenset({pa, pb}) in fail_pairs:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn):
    async def _device(device_id, client=None):
        return SWITCH_DATA if str(device_id) == SWITCH_ID else None

    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]


async def _seed_failed(
    port_a,
    port_b,
    *,
    attempts=3,
    last_error="boom",
    reservation_id=RES_ID,
    intended="ACTIVE",
):
    async with TestSessionLocal() as s:
        row = L1ConnectionAssignment(
            reservation_id=uuid.UUID(reservation_id),
            switch_device_id=uuid.UUID(SWITCH_ID),
            port_a=port_a,
            port_b=port_b,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error=last_error,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_active(port_a, port_b, *, reservation_id=RES_ID):
    async with TestSessionLocal() as s:
        row = L1ConnectionAssignment(
            reservation_id=uuid.UUID(reservation_id),
            switch_device_id=uuid.UUID(SWITCH_ID),
            port_a=port_a,
            port_b=port_b,
            status="ACTIVE",
            intended="ACTIVE",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_state(*, frozen=False, last_applied=None, reservation_id=RES_ID):
    async with TestSessionLocal() as s:
        s.add(
            ReservationWiringState(
                reservation_id=uuid.UUID(reservation_id),
                frozen=frozen,
                last_applied_fork_version=last_applied,
            )
        )
        await s.commit()


async def _rows(status=None):
    async with TestSessionLocal() as s:
        rows = list((await s.execute(select(L1ConnectionAssignment))).scalars().all())
    return [r for r in rows if status is None or r.status == status]


async def _run_reattempt(execute_fn):
    ps = _patches(execute_fn)
    for p in ps:
        p.start()
    try:
        return await reattempt_reservation(RES_ID, _db_session_factory())
    finally:
        for p in ps:
            p.stop()


async def _run_tick(execute_fn):
    ps = _patches(execute_fn)
    for p in ps:
        p.start()
    try:
        return await run_wiring_retry_tick(_db_session_factory())
    finally:
        for p in ps:
            p.stop()


# --- is_retryable_failure classification ------------------------------------


def test_is_retryable_classifies_driver_failures_retryable():
    assert is_retryable_failure("boom") is True
    assert is_retryable_failure("driver login failed: timeout") is True
    assert is_retryable_failure(None) is True
    assert is_retryable_failure("") is True


def test_is_retryable_classifies_pinned_reasons_not_retryable():
    assert is_retryable_failure(WIRING_UNRESOLVABLE_REASON) is False
    assert is_retryable_failure(WIRING_NOT_SIMPLE_CHAIN_REASON) is False
    # The load-error variants share the pinned prefix and are non-retryable too.
    assert is_retryable_failure(f"{WIRING_UNRESOLVABLE_REASON}: switch x not found") is False


# --- reattempt_reservation (manual retry) -----------------------------------


@pytest.mark.asyncio
async def test_retry_flips_failed_build_to_active_on_success():
    """A retryable FAILED build reattempted with a succeeding driver flips ACTIVE in place."""
    fid = await _seed_failed("0/0/1", "0/0/2")
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    active = await _rows("ACTIVE")
    assert len(active) == 1
    assert active[0].id == fid, "the same row is flipped, not a parallel ACTIVE row"
    assert (active[0].port_a, active[0].port_b) == ("0/0/1", "0/0/2")
    assert active[0].last_error is None
    assert await _rows("FAILED") == []
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert result["results"][0]["outcome"] == "reconnected"
    assert result["results"][0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_retry_release_direction_row_drives_disconnect_and_ends_released():
    """Issue #369: a FAILED row whose intended is RELEASED is reattempted as a
    disconnect, not the old hard-coded connect, and ends RELEASED on success."""
    fid = await _seed_failed("0/0/1", "0/0/2", intended="RELEASED")
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert ("connect_ports", "0/0/1", "0/0/2") not in calls, (
        "a release-direction row must not be retried as a connect"
    )
    assert ("disconnect_ports", "0/0/1", "0/0/2") in calls

    released = await _rows("RELEASED")
    assert len(released) == 1
    assert released[0].id == fid, "the same row is flipped, not a parallel row"
    assert released[0].intended == "RELEASED"
    assert released[0].last_error is None
    assert await _rows("FAILED") == []
    assert result["results"][0]["outcome"] == "released"
    assert result["results"][0]["status"] == "RELEASED"


@pytest.mark.asyncio
async def test_retry_release_direction_repeat_failure_stays_failed_released():
    """A release-direction reattempt that fails again stays FAILED, intended
    RELEASED, with attempts accumulated and last_error updated."""
    await _seed_failed("0/0/1", "0/0/2", attempts=3, last_error="old boom", intended="RELEASED")
    execute_fn, calls = _sandbox_recorder(fail_pairs={frozenset({"0/0/1", "0/0/2"})})
    result = await _run_reattempt(execute_fn)

    failed = await _rows("FAILED")
    assert len(failed) == 1
    assert failed[0].intended == "RELEASED"
    assert failed[0].attempts > 3, "attempts accumulate across the reattempt"
    assert failed[0].last_error == "boom"
    assert await _rows("RELEASED") == []
    assert result["results"][0]["outcome"] == "still_failed"
    assert ("disconnect_ports", "0/0/1", "0/0/2") in calls


@pytest.mark.asyncio
async def test_retry_mixed_directions_in_one_reservation():
    """A reservation with both a build-direction and a release-direction FAILED
    row gets each reattempted in its own direction in the same retry call."""
    build_id = await _seed_failed("0/0/1", "0/0/2", intended="ACTIVE")
    release_id = await _seed_failed("0/0/3", "0/0/4", intended="RELEASED")
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert ("disconnect_ports", "0/0/3", "0/0/4") in calls

    outcomes = {r["id"]: r["outcome"] for r in result["results"]}
    assert outcomes[str(build_id)] == "reconnected"
    assert outcomes[str(release_id)] == "released"


@pytest.mark.asyncio
async def test_retry_non_retryable_pinned_reason_makes_no_driver_call():
    """A FAILED row with a pinned reason is reported not_retryable without a driver call."""
    await _seed_failed("0/0/1", "0/0/2", attempts=0, last_error=WIRING_UNRESOLVABLE_REASON)
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert calls == [], "no driver call for a non-retryable intent"
    assert len(await _rows("FAILED")) == 1
    assert result["results"] == [
        {
            "id": result["results"][0]["id"],
            "switch_device_id": SWITCH_ID,
            "port_a": "0/0/1",
            "port_b": "0/0/2",
            "physical_connection_id": None,
            "outcome": "not_retryable",
            "status": "FAILED",
            "attempts": 0,
            "last_error": WIRING_UNRESOLVABLE_REASON,
            "layer": "l1",
        }
    ]


@pytest.mark.asyncio
async def test_retry_frozen_reservation_refuses():
    """A frozen reservation refuses retry (WiringReservationFrozen, mapped to 409)."""
    await _seed_state(frozen=True)
    await _seed_failed("0/0/1", "0/0/2")
    execute_fn, calls = _sandbox_recorder()
    ps = _patches(execute_fn)
    for p in ps:
        p.start()
    try:
        with pytest.raises(WiringReservationFrozen):
            await reattempt_reservation(RES_ID, _db_session_factory())
    finally:
        for p in ps:
            p.stop()
    assert calls == []
    assert len(await _rows("FAILED")) == 1


@pytest.mark.asyncio
async def test_retry_frozen_reservation_processes_releases_reports_builds_frozen():
    """Direction-scoped freeze (issue #369): on a frozen reservation a build-direction
    row is reported "frozen" without a driver call, while a release-direction row is
    still reattempted as a disconnect and ends RELEASED."""
    await _seed_state(frozen=True)
    build_id = await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="ACTIVE")
    release_id = await _seed_failed("0/0/3", "0/0/4", attempts=0, intended="RELEASED")
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert ("connect_ports", "0/0/1", "0/0/2") not in calls, (
        "a build must not be reattempted on a frozen reservation"
    )
    assert ("disconnect_ports", "0/0/3", "0/0/4") in calls

    outcomes = {r["id"]: r["outcome"] for r in result["results"]}
    assert outcomes[str(build_id)] == "frozen"
    assert outcomes[str(release_id)] == "released"

    # The build row stays FAILED; only the release converged.
    failed = {(r.port_a, r.port_b) for r in await _rows("FAILED")}
    assert failed == {("0/0/1", "0/0/2")}
    released = {(r.port_a, r.port_b) for r in await _rows("RELEASED")}
    assert released == {("0/0/3", "0/0/4")}


@pytest.mark.asyncio
async def test_retry_frozen_reservation_only_build_rows_still_raises():
    """A frozen reservation with ONLY build-direction FAILED rows keeps the old refusal
    contract: WiringReservationFrozen, no driver call."""
    await _seed_state(frozen=True)
    await _seed_failed("0/0/1", "0/0/2", intended="ACTIVE")
    await _seed_failed("0/0/3", "0/0/4", intended="ACTIVE")
    execute_fn, calls = _sandbox_recorder()
    ps = _patches(execute_fn)
    for p in ps:
        p.start()
    try:
        with pytest.raises(WiringReservationFrozen):
            await reattempt_reservation(RES_ID, _db_session_factory())
    finally:
        for p in ps:
            p.stop()
    assert calls == []
    assert len(await _rows("FAILED")) == 2


@pytest.mark.asyncio
async def test_retry_supersession_flips_release_row_without_driver_call():
    """Supersession guard (issue #369): a release-direction FAILED row whose port a
    newer build on ANOTHER reservation already reclaimed is settled RELEASED with no
    driver call, reported "superseded"."""
    fid = await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="RELEASED")
    # A different reservation now holds an ACTIVE cross-connect claiming port 0/0/1 on
    # the same switch: the old cross-connect had to be torn down for it to succeed.
    active_id = await _seed_active("0/0/1", "0/0/9", reservation_id=OTHER_RES_ID)
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert calls == [], "a superseded release needs no driver call"
    released = await _rows("RELEASED")
    assert [r.id for r in released] == [fid]
    assert result["results"][0]["outcome"] == "superseded"
    assert result["results"][0]["status"] == "RELEASED"
    # The other reservation's live cross-connect is untouched.
    active = await _rows("ACTIVE")
    assert [r.id for r in active] == [active_id]


@pytest.mark.asyncio
async def test_retry_no_supersession_when_active_row_same_reservation_fires_driver():
    """The supersession guard is cross-reservation: a same-reservation ACTIVE row with
    an overlapping port does NOT supersede, so the disconnect driver still fires."""
    await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="RELEASED")
    # Same reservation holds an ACTIVE row overlapping port 0/0/1: not superseding.
    await _seed_active("0/0/1", "0/0/9", reservation_id=RES_ID)
    execute_fn, calls = _sandbox_recorder()
    result = await _run_reattempt(execute_fn)

    assert ("disconnect_ports", "0/0/1", "0/0/2") in calls, (
        "a same-reservation ACTIVE row must not suppress the disconnect"
    )
    assert result["results"][0]["outcome"] == "released"


@pytest.mark.asyncio
async def test_retry_failed_reattempt_accumulates_attempts_and_updates_error():
    """A reattempt that fails again keeps the row FAILED, grows attempts, updates last_error."""
    await _seed_failed("0/0/1", "0/0/2", attempts=3, last_error="old boom")
    execute_fn, calls = _sandbox_recorder(fail_pairs={frozenset({"0/0/1", "0/0/2"})})
    result = await _run_reattempt(execute_fn)

    failed = await _rows("FAILED")
    assert len(failed) == 1
    assert failed[0].attempts > 3, "attempts accumulate across the reattempt"
    assert failed[0].last_error == "boom"
    assert await _rows("ACTIVE") == []
    assert result["results"][0]["outcome"] == "still_failed"
    assert ("connect_ports", "0/0/1", "0/0/2") in calls


# --- Background auto-retry channel ------------------------------------------


@pytest.mark.asyncio
async def test_tick_respects_batch_cap(monkeypatch):
    """One tick reattempts at most wiring_retry_batch_size FAILED rows."""
    for i in range(5):
        await _seed_failed(f"0/0/{i}", f"0/1/{i}", attempts=0)
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 2)
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["rows_due"] == 2
    assert stats["rows_retried"] == 2
    assert stats["reconnected"] == 2
    # Only two of the five pairs got a connect call this tick.
    connects = [c for c in calls if c[0] == "connect_ports"]
    assert len(connects) == 2
    assert len(await _rows("ACTIVE")) == 2
    assert len(await _rows("FAILED")) == 3


@pytest.mark.asyncio
async def test_tick_reports_released_stat_for_release_direction_rows(monkeypatch):
    """The background tick's stats distinguish reconnected (build) from released
    (release), not just a combined success count."""
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)
    await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="ACTIVE")
    await _seed_failed("0/0/3", "0/0/4", attempts=0, intended="RELEASED")
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["rows_retried"] == 2
    assert stats["reconnected"] == 1
    assert stats["released"] == 1
    assert stats["still_failed"] == 0
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    assert ("disconnect_ports", "0/0/3", "0/0/4") in calls


@pytest.mark.asyncio
async def test_tick_skips_rows_past_max_attempts(monkeypatch):
    """A FAILED row at or above the total-attempts cap is not swept (manual retry only)."""
    monkeypatch.setattr(settings, "wiring_retry_max_attempts", 10)
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)
    await _seed_failed("0/0/1", "0/0/2", attempts=10)  # at cap: excluded
    await _seed_failed("0/0/3", "0/0/4", attempts=0)  # eligible
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["rows_due"] == 1
    assert stats["rows_retried"] == 1
    connects = {(c[1], c[2]) for c in calls if c[0] == "connect_ports"}
    assert ("0/0/3", "0/0/4") in connects
    assert ("0/0/1", "0/0/2") not in connects
    # The parked row is untouched; the eligible one is now ACTIVE.
    active = {(r.port_a, r.port_b) for r in await _rows("ACTIVE")}
    assert active == {("0/0/3", "0/0/4")}


@pytest.mark.asyncio
async def test_tick_skips_frozen_reservation(monkeypatch):
    """A FAILED row whose reservation is frozen is skipped, not reattempted."""
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)
    await _seed_state(frozen=True)
    await _seed_failed("0/0/1", "0/0/2", attempts=0)
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["skipped_frozen"] == 1
    assert stats["rows_retried"] == 0
    assert calls == []
    assert len(await _rows("FAILED")) == 1


@pytest.mark.asyncio
async def test_tick_retries_frozen_release_and_skips_frozen_build(monkeypatch):
    """Direction-scoped freeze in the auto channel (issue #369): a frozen reservation's
    build-direction row is skipped, but its release-direction row is still retried."""
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)
    await _seed_state(frozen=True)
    await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="ACTIVE")
    await _seed_failed("0/0/3", "0/0/4", attempts=0, intended="RELEASED")
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["skipped_frozen"] == 1, "the build-direction row is skipped"
    assert stats["rows_retried"] == 1
    assert stats["released"] == 1
    assert ("connect_ports", "0/0/1", "0/0/2") not in calls
    assert ("disconnect_ports", "0/0/3", "0/0/4") in calls
    assert {(r.port_a, r.port_b) for r in await _rows("RELEASED")} == {("0/0/3", "0/0/4")}
    assert {(r.port_a, r.port_b) for r in await _rows("FAILED")} == {("0/0/1", "0/0/2")}


@pytest.mark.asyncio
async def test_tick_reports_superseded_stat(monkeypatch):
    """The auto channel runs the supersession guard too: a release row a newer build on
    another reservation reclaimed is counted "superseded", no driver call."""
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)
    fid = await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="RELEASED")
    await _seed_active("0/0/1", "0/0/9", reservation_id=OTHER_RES_ID)
    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert stats["rows_due"] == 1
    assert stats["rows_retried"] == 1
    assert stats["superseded"] == 1
    assert stats["released"] == 0
    assert calls == []
    assert [r.id for r in await _rows("RELEASED")] == [fid]


# --- Loop backoff schedule --------------------------------------------------


@pytest.mark.asyncio
async def test_loop_sleeps_base_interval_on_healthy_tick(monkeypatch):
    monkeypatch.setattr(settings, "wiring_retry_interval_seconds", 5)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        raise asyncio.CancelledError

    with (
        patch(
            "app.services.wiring_retry_service.run_wiring_retry_tick",
            new=AsyncMock(return_value={}),
        ),
        patch("app.services.wiring_retry_service.asyncio.sleep", new=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_wiring_retry_loop(_db_session_factory())
    assert sleeps == [5]


@pytest.mark.asyncio
async def test_loop_backs_off_on_tick_failure(monkeypatch):
    monkeypatch.setattr(settings, "wiring_retry_interval_seconds", 5)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        raise asyncio.CancelledError

    with (
        patch(
            "app.services.wiring_retry_service.run_wiring_retry_tick",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch("app.services.wiring_retry_service.asyncio.sleep", new=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_wiring_retry_loop(_db_session_factory())
    assert sleeps == [10], "a failed tick doubles the base interval"


# --- Poller-only / enable split ---------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_start_respects_enable_flag(monkeypatch):
    """start_wiring_retry_scheduler runs only when enabled, honoring the run-mode split."""
    monkeypatch.setattr(settings, "wiring_retry_enabled", False)
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    await start_wiring_retry_scheduler(app)
    assert not hasattr(app.state, "wiring_retry_task")

    monkeypatch.setattr(settings, "wiring_retry_enabled", True)
    await start_wiring_retry_scheduler(app)
    assert getattr(app.state, "wiring_retry_task", None) is not None
    await stop_wiring_retry_scheduler(app)
    assert app.state.wiring_retry_task.cancelled() or app.state.wiring_retry_task.done()


# --- Record-time freeze re-check: the issue #461 interleaving ----------------


@pytest.mark.asyncio
async def test_tick_build_racing_terminal_freeze_does_not_strand_active_row():
    """The exact issue #461 interleaving, pinned at unit level (black-box reproduction
    is unforceable, the PR #414 precedent): the tick selects a FAILED build row while
    frozen is still false, the terminal handler's freeze lands while the build is in
    flight, and the connect succeeds on hardware. The record path must re-check frozen
    and park the row FAILED intended RELEASED instead of flipping it ACTIVE, because
    the teardown pass has already snapshotted the ledger and will never release it."""
    from app.services.l1_assignment_service import (
        FROZEN_BUILD_PENDING_RELEASE,
        freeze_reservation_wiring,
    )

    fid = await _seed_failed("0/0/1", "0/0/2", attempts=0, intended="ACTIVE")

    frozen_landed = {}

    async def _device_then_freeze(device_id, client=None):
        # The switch resolve is awaited after row selection and before the driver call:
        # the deterministic seam for a freeze landing inside the in-flight window.
        if not frozen_landed:
            async with TestSessionLocal() as s:
                await freeze_reservation_wiring(s, uuid.UUID(RES_ID))
            frozen_landed["done"] = True
        return SWITCH_DATA if str(device_id) == SWITCH_ID else None

    execute_fn, calls = _sandbox_recorder()
    ps = [
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(side_effect=_device_then_freeze),
        ),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]
    for p in ps:
        p.start()
    try:
        await run_wiring_retry_tick(_db_session_factory())
    finally:
        for p in ps:
            p.stop()

    # The driver call itself fired (selection saw frozen=false; that is the race).
    assert ("connect_ports", "0/0/1", "0/0/2") in calls
    # But the record path re-checked frozen: no ACTIVE row exists, the same row is
    # parked FAILED intended RELEASED for the release-direction channel.
    assert await _rows("ACTIVE") == []
    failed = await _rows("FAILED")
    assert [r.id for r in failed] == [fid]
    assert failed[0].intended == "RELEASED"
    assert failed[0].last_error == FROZEN_BUILD_PENDING_RELEASE


@pytest.mark.asyncio
async def test_parked_post_freeze_build_converges_released_on_next_tick():
    """The parked row from the #461 interleaving converges: the next tick retries it in
    the release direction (allowed while frozen, ADR 0009 phase 3), the disconnect
    succeeds, and the row ends RELEASED with the hardware clean."""
    from app.services.l1_assignment_service import FROZEN_BUILD_PENDING_RELEASE

    await _seed_state(frozen=True)
    fid = await _seed_failed(
        "0/0/1", "0/0/2", attempts=0, intended="RELEASED", last_error=FROZEN_BUILD_PENDING_RELEASE
    )

    execute_fn, calls = _sandbox_recorder()
    stats = await _run_tick(execute_fn)

    assert ("disconnect_ports", "0/0/1", "0/0/2") in calls
    assert ("connect_ports", "0/0/1", "0/0/2") not in calls
    released = await _rows("RELEASED")
    assert [r.id for r in released] == [fid]
    assert stats["released"] == 1
