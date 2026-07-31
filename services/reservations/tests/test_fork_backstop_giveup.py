"""Fork backstop give-up counter (ADR 0009 phase 7 hardening, issue #448 item 1).

_backstop_missing_forks re-runs a 3-attempt retry backoff every sweep tick for any
ACTIVE topology-carrying reservation cabling has no fork for. Without a cap, a
reservation whose fork create fails PERMANENTLY (e.g. its parent topology was
deleted before any fork ever existed) would consume that backoff on every tick
forever. This suite pins: the cap is enforced, the give-up warning logs exactly
once, a successful create clears the counter, and unrelated reservations are not
cross-contaminated by another reservation's counter.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.tasks import expiration
from app.tasks.expiration import _backstop_missing_forks
from sqlalchemy.ext.asyncio import async_sessionmaker

# _backstop_missing_forks opens its own AsyncSessionLocal against the app engine, so
# this suite must share that engine (mirrors test_fork_archive_reconcile.py).
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # The attempt counter is module-level, in-memory, per-process state (issue #448
    # item 1's deliberate design); reset it around every test so tests do not leak
    # counters into each other.
    expiration._fork_backstop_attempts.clear()
    yield
    expiration._fork_backstop_attempts.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert_active(topology_id: uuid.UUID | None = None) -> uuid.UUID:
    res = Reservation(
        user_id=USER_ID,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_id=topology_id or uuid.uuid4(),
        topology_type=TopologyType.PHYSICAL,
        purpose="t",
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=2),
        status=ReservationStatus.ACTIVE,
    )
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


@pytest.mark.asyncio
async def test_backs_off_after_cap():
    """A permanently failing create is attempted up to the cap, then skipped."""
    rid = await _insert_active()
    create = AsyncMock(return_value=False)
    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        for _ in range(expiration._FORK_BACKSTOP_MAX_ATTEMPTS):
            await _backstop_missing_forks(known_fork_ids=set())
        assert create.await_count == expiration._FORK_BACKSTOP_MAX_ATTEMPTS

        # One more tick past the cap: no further create call.
        await _backstop_missing_forks(known_fork_ids=set())
        assert create.await_count == expiration._FORK_BACKSTOP_MAX_ATTEMPTS

    assert expiration._fork_backstop_attempts[rid] == expiration._FORK_BACKSTOP_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_give_up_logs_once(caplog):
    """The give-up warning fires exactly once, at the tick that hits the cap."""
    await _insert_active()
    create = AsyncMock(return_value=False)
    with (
        patch.object(expiration, "_create_reservation_fork_best_effort", create),
        caplog.at_level(logging.WARNING, logger="app.tasks.expiration"),
    ):
        for _ in range(expiration._FORK_BACKSTOP_MAX_ATTEMPTS + 2):
            await _backstop_missing_forks(known_fork_ids=set())

    give_up_records = [
        r for r in caplog.records if getattr(r, "action", None) == "fork_backstop_give_up"
    ]
    assert len(give_up_records) == 1


@pytest.mark.asyncio
async def test_successful_create_clears_counter():
    """A create that eventually succeeds resets the counter to zero."""
    rid = await _insert_active()
    create = AsyncMock(side_effect=[False, False, True])
    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        await _backstop_missing_forks(known_fork_ids=set())
        await _backstop_missing_forks(known_fork_ids=set())
        assert expiration._fork_backstop_attempts[rid] == 2

        await _backstop_missing_forks(known_fork_ids=set())
        assert create.await_count == 3

    # The successful create cleared the counter entirely (no stale entry left behind).
    assert rid not in expiration._fork_backstop_attempts


@pytest.mark.asyncio
async def test_fork_appearing_via_known_ids_clears_counter():
    """A fork that shows up through another path (known_fork_ids) also clears state."""
    rid = await _insert_active()
    create = AsyncMock(return_value=False)
    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        await _backstop_missing_forks(known_fork_ids=set())
        await _backstop_missing_forks(known_fork_ids=set())
        assert expiration._fork_backstop_attempts[rid] == 2

        # The fork now exists (e.g. the owner lazily created it): the sweep must not
        # call create again and must clear the stale counter.
        await _backstop_missing_forks(known_fork_ids={rid})

    assert create.await_count == 2
    assert rid not in expiration._fork_backstop_attempts


@pytest.mark.asyncio
async def test_different_reservation_unaffected():
    """One reservation giving up does not throttle or affect another's attempts."""
    stuck = await _insert_active()
    healthy = await _insert_active()

    async def fake_create(reservation_id, topology_id, created_by=None):
        return reservation_id == healthy

    create = AsyncMock(side_effect=fake_create)
    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        for _ in range(expiration._FORK_BACKSTOP_MAX_ATTEMPTS + 2):
            await _backstop_missing_forks(known_fork_ids=set())

    # The stuck reservation gave up at the cap and is no longer called.
    assert expiration._fork_backstop_attempts[stuck] == expiration._FORK_BACKSTOP_MAX_ATTEMPTS
    # The healthy reservation succeeded every time and was never throttled or counted.
    assert healthy not in expiration._fork_backstop_attempts

    stuck_calls = sum(1 for c in create.await_args_list if c.args[0] == stuck)
    healthy_calls = sum(1 for c in create.await_args_list if c.args[0] == healthy)
    assert stuck_calls == expiration._FORK_BACKSTOP_MAX_ATTEMPTS
    assert healthy_calls == expiration._FORK_BACKSTOP_MAX_ATTEMPTS + 2


@pytest.mark.asyncio
async def test_stale_counter_pruned_when_reservation_leaves_active():
    """A reservation that accrues a nonzero counter and then leaves the ACTIVE-with-
    topology row set entirely (e.g. the user cancels it before the fork ever succeeds,
    the exact target scenario: parent topology deleted, reservation never fixed) must
    not leak its counter forever. Neither existing pop site fires for a reservation
    that is no longer visited at all, so the top-of-function prune is what clears it.
    A sibling reservation that also has a nonzero counter but stays ACTIVE must keep
    its counter across that same prune: pruning is scoped to the departed key, not a
    blanket clear.
    """
    departs = await _insert_active()
    stays = await _insert_active()

    # Both fail every create; both accrue a counter of 2.
    create = AsyncMock(return_value=False)
    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        await _backstop_missing_forks(known_fork_ids=set())
        await _backstop_missing_forks(known_fork_ids=set())

    assert expiration._fork_backstop_attempts[departs] == 2
    assert expiration._fork_backstop_attempts[stays] == 2

    # `departs` ends (cancelled) before its fork ever succeeded: it drops out of the
    # ACTIVE-with-topology query entirely, so it is never visited by the per-row loop
    # again.
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, departs)
        res.status = ReservationStatus.CANCELLED
        await db.commit()

    with patch.object(expiration, "_create_reservation_fork_best_effort", create):
        await _backstop_missing_forks(known_fork_ids=set())

    assert departs not in expiration._fork_backstop_attempts
    # `stays` is still ACTIVE and still forkless, so it is visited, fails again, and
    # its counter is untouched by the prune (it only advances from the normal path).
    assert expiration._fork_backstop_attempts[stays] == 3
