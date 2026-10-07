"""PATCH on a reservation: hold rule, status guard, add check, and duration cap.

Issue #994: the device-set change commits first under a status guard (the status
the PATCH read must still hold at commit), and only then is inventory written,
with three attempts, removals through the ONE holder-aware release filter. A PATCH
that loses to a cancel or an activation claim keeps nothing; a cancel that commits
after the edit gets the added hold reverted.

Issue #999: adding a device to a PENDING reservation applies create's rule for a
future window: the window conflict check decides, not the device's status now.

Issue #995: PATCH applies RESERVATION_MAX_DURATION_SECONDS to the effective window
with create's own check and wording.

Uses app.database's own engine (like test_expiration_hold_invariant.py) so the one
test that runs the sweep's activation sees the same rows.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import ReservationUpdate
from app.services import reservation_service as svc
from app.services.reservation_service import update_reservation
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

Session = async_sessionmaker(engine, expire_on_commit=False)

SVC = "app.services.reservation_service"
EXP = "app.tasks.expiration"
NOW = datetime.now(timezone.utc)
OWNER = uuid.uuid4()
HELD = uuid.uuid4()
OTHER = uuid.uuid4()

PENDING = ReservationStatus.PENDING
PROVISION = ReservationStatus.PENDING_PROVISION
ACTIVE = ReservationStatus.ACTIVE
CANCELLED = ReservationStatus.CANCELLED


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _device(device_id, status="AVAILABLE", exclusive=True):
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "topology_type": "PHYSICAL",
        "status": status,
        "exclusive": exclusive,
    }


async def _insert(status, device_ids, *, user_id=OWNER, start=None, end=None):
    rid = uuid.uuid4()
    if start is None:
        start = NOW - timedelta(minutes=5) if status == ACTIVE else NOW + timedelta(hours=1)
    if end is None:
        end = start + timedelta(hours=2)
    async with Session() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=user_id,
                device_ids=[str(d) for d in device_ids],
                topology_type=TopologyType.PHYSICAL,
                purpose="t",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
        await s.commit()
    return rid


async def _row(rid):
    async with Session() as s:
        return (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()


async def _subjects():
    async with Session() as s:
        rows = (await s.execute(select(OutboxEvent))).scalars().all()
    return sorted(r.subject for r in rows)


async def _set_status_on_second_session(rid, status):
    async with Session() as other:
        await other.execute(update(Reservation).where(Reservation.id == rid).values(status=status))
        await other.commit()


class Inventory:
    """Fake inventory status write: records calls, optionally fails or runs a hook."""

    def __init__(self, *, fail=False, on_reserved=None):
        self.calls: list[tuple[list[str], str]] = []
        self.fail = fail
        self.on_reserved = on_reserved

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        self.calls.append(([str(i) for i in ids], status))
        if self.fail:
            if raise_on_failure:
                raise RuntimeError("inventory 503")
            return []
        if succeeded is not None:
            succeeded.update(ids)
        if status == "RESERVED" and self.on_reserved is not None:
            await self.on_reserved()
        return list(ids)


@pytest.fixture
def seams():
    """Stub the HTTP seams; retries keep their attempt count but do not sleep."""
    real_retry = svc.retry_with_backoff

    async def no_sleep_retry(fn, **kw):
        return await real_retry(fn, **{**kw, "initial_delay": 0, "max_delay": 0})

    async def fetch(ids, token):
        return [_device(d) for d in ids]

    async def best_effort(ids):
        return [_device(d) for d in ids]

    with (
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._fetch_devices_best_effort", new=best_effort),
        patch(f"{SVC}._prune_removed_devices_from_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}.retry_with_backoff", new=no_sleep_retry),
    ):
        yield


async def _patch(rid, **body):
    async with Session() as db:
        return await update_reservation(db, rid, OWNER, ReservationUpdate(**body), token="t")


def _actions(caplog, action):
    return [r for r in caplog.records if getattr(r, "action", None) == action]


# --- issue #995: the duration cap applies to the effective window ---

CAP = 7200


@pytest.fixture
def cap():
    with patch("app.config.settings.reservation_max_duration_seconds", CAP):
        yield CAP


@pytest.mark.parametrize(
    "extra_seconds,ok",
    [(0, True), (1, False), (400 * 24 * 3600, False)],
    ids=["exactly-the-cap", "cap-plus-one-second", "far-over"],
)
async def test_patch_end_time_is_judged_against_the_cap(seams, cap, extra_seconds, ok):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(hours=1))
    new_end = start + timedelta(seconds=CAP + extra_seconds)
    with patch(f"{SVC}._update_device_statuses", new=Inventory()):
        if ok:
            out = await _patch(rid, end_time=new_end)
            assert out.end_time.replace(tzinfo=timezone.utc) == new_end
        else:
            with pytest.raises(ValueError) as info:
                await _patch(rid, end_time=new_end)
            assert str(info.value) == f"reservation duration exceeds the maximum of {CAP}s"
            assert await _subjects() == []


async def test_patch_cap_uses_the_stored_start_on_an_active_row(seams, cap):
    """An ACTIVE row's window is measured from its stored start, not from now."""
    start = NOW - timedelta(minutes=30)
    rid = await _insert(ACTIVE, [HELD], start=start, end=start + timedelta(hours=1))
    with (
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
        pytest.raises(ValueError, match=f"maximum of {CAP}s"),
    ):
        await _patch(rid, end_time=start + timedelta(seconds=CAP + 1))


async def test_over_cap_legacy_row_stays_editable_outside_its_window(seams, cap):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(seconds=CAP * 5))
    with patch(f"{SVC}._update_device_statuses", new=Inventory()):
        out = await _patch(rid, purpose="renamed", device_ids=[HELD, OTHER])
    assert out.purpose == "renamed"
    assert {str(d) for d in out.device_ids} == {str(HELD), str(OTHER)}


async def test_over_cap_legacy_row_cannot_set_a_window_still_over_the_cap(seams, cap):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(seconds=CAP * 5))
    with (
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
        pytest.raises(ValueError, match=f"maximum of {CAP}s"),
    ):
        await _patch(rid, end_time=start + timedelta(seconds=CAP * 2))


async def test_cap_zero_disables_the_patch_check(seams):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(hours=1))
    with (
        patch("app.config.settings.reservation_max_duration_seconds", 0),
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
    ):
        out = await _patch(rid, end_time=start + timedelta(days=400))
    assert out.end_time.replace(tzinfo=timezone.utc) == start + timedelta(days=400)
