"""Unit tests for the fleet-scale health polling additions (issue #24).

Covers the four workstreams: batch size and bounded concurrency (run_tick's
claim LIMIT and semaphore), event-driven tier transitions (persisted
poll_tier plus interval override resolution), the poller-only run mode
(router mounting), and the per-tick structured log. Same in-memory sqlite
shape as test_health_scheduler.py; fire_poll is stubbed with event-gated
fakes so concurrency invariants are asserted deterministically, never with
wall-clock sleeps.
"""

import asyncio
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.device_health_status import DeviceHealthStatus
from app.services import health_scheduler
from app.services.health_scheduler import (
    TIER_IDLE,
    TIER_IN_USE,
    _effective_interval,
    apply_reservation_event_tiers,
    apply_tier_transition,
    run_tick,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# File-backed sqlite, unlike the :memory: engine the sibling suites use: the
# concurrency tests here run genuinely overlapping sessions, and aiosqlite
# :memory: shares one connection (StaticPool) across sessions, which cannot
# host two interleaved transactions. A file gives each session its own
# connection, matching the concurrent-poller shape under test.
_db_fd, _db_path = tempfile.mkstemp(prefix="herd_health_scale_", suffix=".sqlite")
os.close(_db_fd)
engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_db_file():
    yield
    if os.path.exists(_db_path):
        os.unlink(_db_path)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    health_scheduler._registry.clear()
    # Mark the registry fresh so run_tick never tries a real inventory fetch.
    health_scheduler._registry_last_refresh = datetime.now(timezone.utc)
    health_scheduler._template_cache.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    health_scheduler._registry.clear()
    health_scheduler._registry_last_refresh = None
    health_scheduler._template_cache.clear()


async def _seed_row(
    *,
    next_poll_at: datetime,
    poll_tier: str = TIER_IDLE,
    registry_interval: int | None = 60,
) -> uuid.UUID:
    device_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            DeviceHealthStatus(
                device_id=device_id,
                next_poll_at=next_poll_at,
                poll_tier=poll_tier,
            )
        )
        await db.commit()
    if registry_interval is not None:
        health_scheduler._registry[device_id] = registry_interval
    return device_id


def _as_utc(dt: datetime) -> datetime:
    # SQLite drops tzinfo on read; tag UTC for comparisons.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _get_row(device_id: uuid.UUID) -> DeviceHealthStatus:
    async with TestSessionLocal() as db:
        return await db.get(DeviceHealthStatus, device_id)


# --- Config defaults pin the "preserve current behavior" contract ---


def test_fleet_scale_defaults_match_pre_24_behavior():
    """batch 10 (the old hardcoded LIMIT), concurrency 1 (the old sequential
    firing), tier overrides off, full API mounted."""
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["health_poll_batch_size"].default == 10
    assert fields["health_poll_max_concurrency"].default == 1
    assert fields["health_poll_in_use_interval_seconds"].default == 0
    assert fields["health_poll_idle_interval_seconds"].default == 0
    assert fields["execution_poller_only"].default is False


# --- Batch size ---


@pytest.mark.asyncio
async def test_run_tick_claims_at_most_batch_size(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_batch_size", 3)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    fired: list[uuid.UUID] = []

    async def stub(session_factory, device_id, interval, nc=None):
        fired.append(device_id)

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    stats = await run_tick(TestSessionLocal, client=None)

    # rows_due reports the full backlog; only the batch is claimed and fired.
    assert stats["rows_due"] == 5
    assert stats["rows_claimed"] == 3
    assert stats["polls_fired"] == 3
    assert len(fired) == 3


# --- Bounded concurrency ---


@pytest.mark.asyncio
async def test_run_tick_bounds_concurrency_to_k(monkeypatch):
    """With N=5 due and k=2, at most 2 polls are in flight at once; the other
    3 report as deferred and still all fire. The stub holds the first two
    polls open behind an Event, so the max-in-flight invariant is observed at
    a deterministic point rather than inferred from timing."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_max_concurrency", 2)
    monkeypatch.setattr(health_scheduler.settings, "health_poll_batch_size", 10)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    in_flight = 0
    max_in_flight = 0
    fired: list[uuid.UUID] = []
    release = asyncio.Event()
    two_running = asyncio.Event()

    async def stub(session_factory, device_id, interval, nc=None):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        fired.append(device_id)
        if in_flight == 2:
            two_running.set()
        await release.wait()
        in_flight -= 1

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    tick = asyncio.create_task(run_tick(TestSessionLocal, client=None))

    await asyncio.wait_for(two_running.wait(), timeout=5)
    # Two polls hold the semaphore open right now; nothing else got in.
    assert in_flight == 2
    release.set()
    stats = await asyncio.wait_for(tick, timeout=5)

    assert max_in_flight == 2
    assert len(fired) == 5
    assert stats["polls_fired"] == 5
    assert stats["polls_deferred"] == 3


@pytest.mark.asyncio
async def test_run_tick_default_concurrency_is_sequential(monkeypatch):
    """k=1 (the default): polls never overlap, matching pre-#24 behavior."""
    now = datetime.now(timezone.utc)
    for _ in range(3):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    in_flight = 0
    max_in_flight = 0

    async def stub(session_factory, device_id, interval, nc=None):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    stats = await run_tick(TestSessionLocal, client=None)

    assert max_in_flight == 1
    assert stats["polls_fired"] == 3


# --- Claim arbitration across ticks and instances ---


@pytest.mark.asyncio
async def test_sequential_ticks_do_not_double_poll(monkeypatch):
    """A claimed row is pushed past `now`, so a second tick finds nothing."""
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now - timedelta(seconds=10))

    fired: list[uuid.UUID] = []

    async def stub(session_factory, did, interval, nc=None):
        fired.append(did)

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    first = await run_tick(TestSessionLocal, client=None)
    second = await run_tick(TestSessionLocal, client=None)

    assert fired == [device_id]
    assert first["polls_fired"] == 1
    assert second["rows_due"] == 0
    assert second["polls_fired"] == 0


@pytest.mark.asyncio
async def test_concurrent_ticks_claim_each_row_exactly_once(monkeypatch):
    """Two poller instances against one schema: the conditional-update claim
    arbitrates, so every device fires exactly once across both ticks.

    Scope note: SQLite ignores FOR UPDATE SKIP LOCKED, so this pins the
    conditional-UPDATE guard (the correctness guarantee) only; the SKIP
    LOCKED fast path is exercised on the live Postgres stack."""
    now = datetime.now(timezone.utc)
    ids = {await _seed_row(next_poll_at=now - timedelta(seconds=10)) for _ in range(3)}

    fired: list[uuid.UUID] = []

    async def stub(session_factory, did, interval, nc=None):
        fired.append(did)
        await asyncio.sleep(0)

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    stats_a, stats_b = await asyncio.gather(
        run_tick(TestSessionLocal, client=None),
        run_tick(TestSessionLocal, client=None),
    )

    assert sorted(str(d) for d in fired) == sorted(str(d) for d in ids)
    assert stats_a["polls_fired"] + stats_b["polls_fired"] == 3


@pytest.mark.asyncio
async def test_run_tick_skips_device_dropped_from_registry(monkeypatch):
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now - timedelta(seconds=10), registry_interval=None)

    stub = AsyncMock()
    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    stats = await run_tick(TestSessionLocal, client=None)

    stub.assert_not_awaited()
    assert stats["polls_fired"] == 0
    row = await _get_row(device_id)
    # Pushed out by roughly the registry refresh interval so it does not
    # reappear every tick.
    assert _as_utc(row.next_poll_at) > now + timedelta(seconds=100)


# --- Tier interval resolution ---


def test_effective_interval_defaults_resolve_registry_interval(monkeypatch):
    """Both overrides at 0 (default): every tier resolves exactly the
    registry interval, the pre-#24 cadence."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 0)
    monkeypatch.setattr(health_scheduler.settings, "health_poll_idle_interval_seconds", 0)
    assert _effective_interval(60, TIER_IDLE) == 60
    assert _effective_interval(60, TIER_IN_USE) == 60
    assert _effective_interval(60, None) == 60


def test_effective_interval_per_tier_overrides(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 15)
    monkeypatch.setattr(health_scheduler.settings, "health_poll_idle_interval_seconds", 600)
    assert _effective_interval(60, TIER_IN_USE) == 15
    assert _effective_interval(60, TIER_IDLE) == 600
    # Unknown/NULL tier behaves as idle, matching the column default.
    assert _effective_interval(60, None) == 600


def test_effective_interval_single_override(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 15)
    monkeypatch.setattr(health_scheduler.settings, "health_poll_idle_interval_seconds", 0)
    assert _effective_interval(60, TIER_IN_USE) == 15
    assert _effective_interval(60, TIER_IDLE) == 60


@pytest.mark.asyncio
async def test_run_tick_fires_with_tier_resolved_interval(monkeypatch):
    """An in-use device polls on the in-use cadence, not the registry one."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 10)
    now = datetime.now(timezone.utc)
    in_use_id = await _seed_row(
        next_poll_at=now - timedelta(seconds=10), poll_tier=TIER_IN_USE, registry_interval=60
    )
    idle_id = await _seed_row(
        next_poll_at=now - timedelta(seconds=10), poll_tier=TIER_IDLE, registry_interval=60
    )

    intervals: dict[uuid.UUID, int] = {}

    async def stub(session_factory, device_id, interval, nc=None):
        intervals[device_id] = interval

    monkeypatch.setattr(health_scheduler, "fire_poll", stub)
    await run_tick(TestSessionLocal, client=None)

    assert intervals[in_use_id] == 10
    assert intervals[idle_id] == 60


# --- Tier transitions ---


@pytest.mark.asyncio
async def test_in_use_transition_shortens_next_poll(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 60)
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(hours=1))

    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IN_USE)

    row = await _get_row(device_id)
    assert row.poll_tier == TIER_IN_USE
    nxt = _as_utc(row.next_poll_at)
    assert nxt <= now + timedelta(seconds=61)
    assert nxt > now


@pytest.mark.asyncio
async def test_in_use_transition_never_delays_a_due_row(monkeypatch):
    """Pulling earlier only: a row already due stays due."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 60)
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now - timedelta(seconds=30))

    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IN_USE)

    row = await _get_row(device_id)
    assert row.poll_tier == TIER_IN_USE
    assert _as_utc(row.next_poll_at) <= now


@pytest.mark.asyncio
async def test_in_use_transition_with_default_config_leaves_schedule(monkeypatch):
    """Override at 0 (default): the tier flips but the schedule is untouched,
    so default config preserves the pre-#24 cadence exactly."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 0)
    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(hours=1)
    device_id = await _seed_row(next_poll_at=scheduled)

    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IN_USE)

    row = await _get_row(device_id)
    assert row.poll_tier == TIER_IN_USE
    assert abs((_as_utc(row.next_poll_at) - scheduled).total_seconds()) < 1


@pytest.mark.asyncio
async def test_idle_transition_sets_tier_only(monkeypatch):
    monkeypatch.setattr(health_scheduler.settings, "health_poll_idle_interval_seconds", 600)
    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(seconds=30)
    device_id = await _seed_row(next_poll_at=scheduled, poll_tier=TIER_IN_USE)

    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IDLE)

    row = await _get_row(device_id)
    assert row.poll_tier == TIER_IDLE
    # next_poll_at untouched; the idle cadence applies from the next poll.
    assert abs((_as_utc(row.next_poll_at) - scheduled).total_seconds()) < 1


@pytest.mark.asyncio
async def test_in_use_transition_inserts_missing_row():
    """A device reserved before its first registry seed still gets the in-use
    tier (and an immediately-due row) instead of idling all reservation."""
    device_id = uuid.uuid4()

    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IN_USE)

    row = await _get_row(device_id)
    assert row is not None
    assert row.poll_tier == TIER_IN_USE


@pytest.mark.asyncio
async def test_tier_survives_restart_via_persistence():
    """The tier is a column, not process state: a fresh session (standing in
    for a restarted service) reads the same tier back."""
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(minutes=5))
    await apply_tier_transition(TestSessionLocal, [str(device_id)], TIER_IN_USE)

    fresh_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with fresh_factory() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.poll_tier == TIER_IN_USE


# --- Event mapping ---


@pytest.mark.asyncio
async def test_reservation_created_moves_devices_in_use():
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(minutes=5))
    event = {"reservation_id": str(uuid.uuid4()), "device_ids": [str(device_id)]}

    await apply_reservation_event_tiers(TestSessionLocal, "reservation.created", event)

    assert (await _get_row(device_id)).poll_tier == TIER_IN_USE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    ["reservation.completed", "reservation.cancelled", "reservation.failed"],
)
async def test_terminal_events_move_devices_idle(event_type):
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(minutes=5), poll_tier=TIER_IN_USE)
    event = {"reservation_id": str(uuid.uuid4()), "device_ids": [str(device_id)]}

    await apply_reservation_event_tiers(TestSessionLocal, event_type, event)

    assert (await _get_row(device_id)).poll_tier == TIER_IDLE


@pytest.mark.asyncio
async def test_reservation_updated_splits_kept_and_removed():
    now = datetime.now(timezone.utc)
    kept = await _seed_row(next_poll_at=now + timedelta(minutes=5))
    removed = await _seed_row(next_poll_at=now + timedelta(minutes=5), poll_tier=TIER_IN_USE)
    event = {
        "reservation_id": str(uuid.uuid4()),
        "device_ids": [str(kept)],
        "removed_device_ids": [str(removed)],
    }

    await apply_reservation_event_tiers(TestSessionLocal, "reservation.updated", event)

    assert (await _get_row(kept)).poll_tier == TIER_IN_USE
    assert (await _get_row(removed)).poll_tier == TIER_IDLE


@pytest.mark.asyncio
async def test_unrelated_event_changes_nothing():
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(minutes=5))

    await apply_reservation_event_tiers(
        TestSessionLocal,
        "reservation.provision_requested",
        {"device_ids": [str(device_id)]},
    )

    assert (await _get_row(device_id)).poll_tier == TIER_IDLE


@pytest.mark.asyncio
async def test_handle_reservation_event_applies_tiers():
    """The consumer drives the tier transition: handle_reservation_event hands
    the event to apply_reservation_event_tiers before provisioning."""
    from app.services.nats_consumer import handle_reservation_event

    event = {
        "event": "reservation.completed",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": [str(uuid.uuid4())],
    }
    with (
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
        ) as tiers,
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_l3_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ),
    ):
        session_ctx = AsyncMock()
        await handle_reservation_event(event, session_ctx)
        tiers.assert_awaited_once_with(session_ctx, "reservation.completed", event)


@pytest.mark.asyncio
async def test_handle_reservation_event_survives_tier_failure():
    """A tier-transition failure is swallowed: provisioning still runs and the
    handler does not raise (so the message is not NAK'd for a cadence hint)."""
    from app.services.nats_consumer import handle_reservation_event

    event = {
        "event": "reservation.completed",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": [str(uuid.uuid4())],
    }
    with (
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tier update broke"),
        ),
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as l1,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_l3_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ),
    ):
        await handle_reservation_event(event, AsyncMock())
        l1.assert_awaited_once()


# --- Per-tick structured log ---


@pytest.mark.asyncio
async def test_tick_emits_structured_log(monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    for _ in range(2):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    monkeypatch.setattr(health_scheduler, "fire_poll", AsyncMock())
    with caplog.at_level(logging.INFO, logger="app.services.health_scheduler"):
        await run_tick(TestSessionLocal, client=None)

    ticks = [r for r in caplog.records if r.getMessage() == "health_tick"]
    assert len(ticks) == 1
    record = ticks[0]
    assert record.rows_due == 2
    assert record.rows_claimed == 2
    assert record.polls_fired == 2
    # k=1 (default): the second poll had to wait behind the semaphore.
    assert record.polls_deferred == 1


# --- Poller-only run mode ---


def test_mount_api_routers_default_mounts_everything(monkeypatch):
    from app import main as main_module
    from fastapi import FastAPI

    monkeypatch.setattr(main_module.settings, "execution_poller_only", False)
    app = FastAPI()
    baseline = len(app.routes)
    assert main_module.mount_api_routers(app) is True
    assert len(app.routes) > baseline


def test_mount_api_routers_poller_only_mounts_nothing(monkeypatch):
    from app import main as main_module
    from fastapi import FastAPI

    monkeypatch.setattr(main_module.settings, "execution_poller_only", True)
    app = FastAPI()
    baseline = len(app.routes)
    assert main_module.mount_api_routers(app) is False
    assert len(app.routes) == baseline


# --- QA-pass regressions (post-review hardening) ---


@pytest.mark.asyncio
async def test_in_use_transition_survives_seeder_insert_race():
    """The registry seeder inserting the row between the tier UPDATE and the
    missing-row INSERT must not cost the device its in-use tier: the handler
    re-applies the tier UPDATE against the row that won the insert race,
    instead of swallowing the conflict and leaving the device idle for the
    whole reservation."""
    device_id = uuid.uuid4()
    commits = {"n": 0}

    @asynccontextmanager
    async def racing_factory():
        async with TestSessionLocal() as db:
            real_commit = db.commit

            async def commit():
                commits["n"] += 1
                if commits["n"] == 2:
                    # Commit 1 is the bulk tier UPDATE; commit 2 is the
                    # missing-row INSERT. The seeder's idle row lands here,
                    # after the existence SELECT saw nothing, so the INSERT
                    # hits a real primary-key conflict.
                    async with TestSessionLocal() as other:
                        other.add(
                            DeviceHealthStatus(
                                device_id=device_id,
                                next_poll_at=datetime.now(timezone.utc),
                                poll_tier=TIER_IDLE,
                            )
                        )
                        await other.commit()
                await real_commit()

            db.commit = commit
            yield db

    await apply_tier_transition(racing_factory, [str(device_id)], TIER_IN_USE)

    row = await _get_row(device_id)
    assert row is not None
    assert row.poll_tier == TIER_IN_USE
    # Three commits: UPDATE, failed INSERT, re-applied UPDATE.
    assert commits["n"] == 3


@pytest.mark.asyncio
async def test_reservation_event_redelivery_is_idempotent(monkeypatch):
    """A redelivered lifecycle event re-applies the same tier and does not
    move an already-shortened schedule (the CASE only ever pulls earlier)."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_in_use_interval_seconds", 60)
    now = datetime.now(timezone.utc)
    device_id = await _seed_row(next_poll_at=now + timedelta(hours=1))
    event = {"device_ids": [str(device_id)]}

    await apply_reservation_event_tiers(TestSessionLocal, "reservation.created", event)
    first = await _get_row(device_id)
    first_next = _as_utc(first.next_poll_at)
    assert first.poll_tier == TIER_IN_USE

    await apply_reservation_event_tiers(TestSessionLocal, "reservation.created", event)
    second = await _get_row(device_id)
    assert second.poll_tier == TIER_IN_USE
    assert abs((_as_utc(second.next_poll_at) - first_next).total_seconds()) < 1


@pytest.mark.asyncio
async def test_rows_due_skips_count_when_batch_not_full(monkeypatch):
    """A partial batch means the LIMIT did not bind, so len(due) is already
    the exact total and the O(backlog) COUNT query is skipped entirely."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_batch_size", 5)
    now = datetime.now(timezone.utc)
    for _ in range(2):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    calls = {"n": 0}
    real_count = health_scheduler._due_count

    async def counting(db, ts):
        calls["n"] += 1
        return await real_count(db, ts)

    monkeypatch.setattr(health_scheduler, "_due_count", counting)
    monkeypatch.setattr(health_scheduler, "fire_poll", AsyncMock())
    stats = await run_tick(TestSessionLocal, client=None)

    assert stats["rows_due"] == 2
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_rows_due_counts_backlog_when_batch_full(monkeypatch):
    """A full batch may hide backlog, so the unbounded COUNT runs and
    rows_due reports the true total, not the batch size."""
    monkeypatch.setattr(health_scheduler.settings, "health_poll_batch_size", 2)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        await _seed_row(next_poll_at=now - timedelta(seconds=10))

    monkeypatch.setattr(health_scheduler, "fire_poll", AsyncMock())
    stats = await run_tick(TestSessionLocal, client=None)

    assert stats["rows_due"] == 3
    assert stats["polls_fired"] == 2


def test_concurrency_over_thread_pool_warns(monkeypatch, caplog):
    """max_concurrency above the shared default executor cannot actually run
    in parallel; the scheduler must call the foot-gun out at startup."""
    cap = health_scheduler._default_executor_cap()
    monkeypatch.setattr(health_scheduler.settings, "health_poll_max_concurrency", cap + 1)
    with caplog.at_level(logging.WARNING, logger="app.services.health_scheduler"):
        health_scheduler._warn_if_concurrency_exceeds_pool()
    assert any("health_poll_max_concurrency" in r.getMessage() for r in caplog.records)


def test_concurrency_at_pool_size_does_not_warn(monkeypatch, caplog):
    cap = health_scheduler._default_executor_cap()
    monkeypatch.setattr(health_scheduler.settings, "health_poll_max_concurrency", cap)
    with caplog.at_level(logging.WARNING, logger="app.services.health_scheduler"):
        health_scheduler._warn_if_concurrency_exceeds_pool()
    assert not caplog.records
