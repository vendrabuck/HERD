"""Postgres-live proof that a reservation PATCH obeys the hold rule under a racing cancel.

Issue #994. `update_reservation` read the row's status once, spent seconds on
inventory and cabling HTTP calls, wrote inventory BEFORE committing, and committed
with no status check, so a cancel that committed during the PATCH still got the
added device set RESERVED (a device held by nobody) and a `reservation.updated`
staged on a terminal row. The edit now commits under a self-transition status
compare-and-swap and writes inventory only after the commit; a cancel that commits
after the edit gets the added hold reverted.

test_reservation_patch_hold.py pins each path deterministically on SQLite, which
never contends for a row and shares one connection between sessions. This file runs
PATCH and cancel as REAL concurrent asyncio tasks over SEPARATE Postgres sessions,
through the real production functions:

  - cancel during the PATCH's device fetch (before its commit): the PATCH must keep
    nothing, stage nothing, and write no RESERVED;
  - cancel during the PATCH's post-commit RESERVED flip: the added device's last
    inventory write must be AVAILABLE;
  - a jittered loop firing the cancel at offsets straddling the PATCH's commit, so
    the row lock itself is contended.

Asserted in every case: the row ends CANCELLED, exactly one `reservation.cancelled`
is staged, a `reservation.updated` exists only when the PATCH returned, and the
added device is never left with RESERVED as its last write.

Env contract and gate-ledger scoping are identical to
test_reservation_status_cas_live_pg.py (issue #819): every test owns a random user
and random device ids, snapshots every other reservation before and after, and
deletes its own rows and outbox events in a finally. Rows end an hour ahead so a
live sweeper never touches them.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import ReservationUpdate
from app.services.reservation_service import (
    ReservationStatusChanged,
    cancel_reservation,
    update_reservation,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")

_CONNECT_ARGS = {"server_settings": {"search_path": "reservations"}}


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


async def _pg_reachable() -> bool:
    engine = _make_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


_PG_REACHABLE = asyncio.run(_pg_reachable())

pytestmark = pytest.mark.skipif(
    not _PG_REQUIRED and not _PG_REACHABLE,
    reason=(
        f"No Postgres reachable at {PG_DSN!r}; set HERD_TEST_PG_DSN to point at one "
        "(e.g. the gate stack's published postgres port, 5433) to run this suite."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _PG_REQUIRED and not _PG_REACHABLE:
        pytest.fail(
            f"HERD_TEST_PG_REQUIRED is set but no Postgres is reachable at {PG_DSN!r}; "
            "start one or unset HERD_TEST_PG_REQUIRED."
        )


SVC = "app.services.reservation_service"
ACTIVE = ReservationStatus.ACTIVE
CANCELLED = ReservationStatus.CANCELLED

# Long enough that a task started inside a slow step provably lands inside it,
# short enough to keep the suite quick.
SLOW_SECONDS = 0.4


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def scope(session_factory):
    """Per-test ownership: a random user and device set, cleaned up in the finally.

    Yields an object carrying the ids, a `foreign_snapshot()` for the untouched-rows
    assertion, and `events()` scoped to this test's reservations.
    """

    class Scope:
        user_id = uuid.uuid4()
        device_ids = [uuid.uuid4(), uuid.uuid4()]
        started = datetime.now(timezone.utc)

        async def my_ids(self):
            async with session_factory() as s:
                return set(
                    (
                        await s.execute(
                            select(Reservation.id).where(Reservation.user_id == self.user_id)
                        )
                    )
                    .scalars()
                    .all()
                )

        async def foreign_snapshot(self):
            async with session_factory() as s:
                rows = await s.execute(
                    select(Reservation.id, Reservation.status).where(
                        Reservation.user_id != self.user_id
                    )
                )
                return {r.id: r.status for r in rows}

        async def events(self, subject=None):
            mine = {str(i) for i in await self.my_ids()}
            async with session_factory() as s:
                stmt = select(OutboxEvent).where(OutboxEvent.created_at >= self.started)
                if subject:
                    stmt = stmt.where(OutboxEvent.subject == subject)
                rows = (await s.execute(stmt)).scalars().all()
            return [r for r in rows if r.payload.get("reservation_id") in mine]

    sc = Scope()
    before = await sc.foreign_snapshot()
    try:
        yield sc, before
    finally:
        mine = [str(i) for i in await sc.my_ids()]
        async with session_factory() as s:
            if mine:
                await s.execute(
                    text("DELETE FROM reservation_devices WHERE reservation_id = ANY(:ids)"),
                    {"ids": [uuid.UUID(i) for i in mine]},
                )
                await s.execute(
                    text("DELETE FROM outbox WHERE payload->>'reservation_id' = ANY(:ids)"),
                    {"ids": mine},
                )
                await s.execute(
                    text("DELETE FROM reservations WHERE id = ANY(:ids)"),
                    {"ids": [uuid.UUID(i) for i in mine]},
                )
            await s.commit()


def _device(device_id):
    return {
        "id": str(device_id),
        "name": f"live-{str(device_id)[:6]}",
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "exclusive": True,
    }


class Inventory:
    """Fake inventory status write; `flipping` is set once a RESERVED write began.

    A call is recorded when its simulated write LANDS, after the RESERVED sleep,
    not when it starts (issue #1136). A cancel that releases the row's devices
    while the flip sleeps is therefore recorded BEFORE the RESERVED write, so the
    added device's last write is AVAILABLE only when the PATCH's own re-read and
    revert put it back.
    """

    def __init__(self, flip_seconds=0.0):
        self.calls: list[tuple[list[str], str]] = []
        self.flipping = asyncio.Event()
        self.flip_seconds = flip_seconds

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        if status == "RESERVED":
            self.flipping.set()
            await asyncio.sleep(self.flip_seconds)
        self.calls.append(([str(i) for i in ids], status))
        if succeeded is not None:
            succeeded.update(ids)
        return list(ids)

    def last_write_for(self, device_id):
        writes = [st for ids, st in self.calls if str(device_id) in ids]
        return writes[-1] if writes else None


async def _insert_active(session_factory, sc, device_ids):
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with session_factory() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=sc.user_id,
                device_ids=[str(d) for d in device_ids],
                topology_type=TopologyType.PHYSICAL,
                purpose="live pg patch",
                start_time=now - timedelta(minutes=5),
                end_time=now + timedelta(hours=1),
                status=ACTIVE,
            )
        )
        await s.commit()
    return rid


async def _status_of(session_factory, rid):
    async with session_factory() as s:
        return (
            await s.execute(select(Reservation.status).where(Reservation.id == rid))
        ).scalar_one()


async def _events_for(sc, rid, subject):
    return [e for e in await sc.events(subject) if e.payload["reservation_id"] == str(rid)]


async def _patch_versus_cancel(session_factory, sc, *, fetch_seconds, flip_seconds, cancel_on):
    """Run a PATCH-add and a cancel on one ACTIVE row as concurrent tasks.

    cancel_on is ("fetch", delay) to fire `delay` seconds after the PATCH's device
    fetch began, or ("flip", delay) to fire after its post-commit RESERVED write
    began. Returns (rid, held, added, patch outcome, cancel result, inventory).
    """
    held, added = uuid.uuid4(), uuid.uuid4()
    rid = await _insert_active(session_factory, sc, [held])
    inv = Inventory(flip_seconds=flip_seconds)
    fetching = asyncio.Event()

    async def slow_fetch(ids, token):
        fetching.set()
        await asyncio.sleep(fetch_seconds)
        return [_device(d) for d in ids]

    async def best_effort(ids):
        return [_device(d) for d in ids]

    async def do_patch():
        async with session_factory() as db:
            try:
                return await update_reservation(
                    db, rid, sc.user_id, ReservationUpdate(device_ids=[held, added]), token="t"
                )
            except ReservationStatusChanged as exc:
                return exc

    async def do_cancel():
        where, delay = cancel_on
        await (fetching if where == "fetch" else inv.flipping).wait()
        if delay:
            await asyncio.sleep(delay)
        async with session_factory() as db:
            return await cancel_reservation(db, rid, sc.user_id, "tok")

    svc = "app.services.reservation_service"
    with (
        patch(f"{svc}._fetch_devices", new=slow_fetch),
        patch(f"{svc}._fetch_devices_best_effort", new=best_effort),
        patch(f"{svc}._update_device_statuses", new=inv),
        patch(f"{svc}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{svc}._prune_removed_devices_from_fork_best_effort", new=AsyncMock()),
    ):
        if cancel_on[0] == "flip":
            # The flip may never start if the cancel is meant to land after it; a
            # cancel waiting on it would hang, so release it when the PATCH ends.
            patch_task = asyncio.create_task(do_patch())
            patch_task.add_done_callback(lambda _t: inv.flipping.set())
            patched, cancelled = await asyncio.gather(patch_task, do_cancel())
        else:
            patched, cancelled = await asyncio.gather(do_patch(), do_cancel())
    return rid, held, added, patched, cancelled, inv


async def test_cancel_during_the_patch_checks_keeps_nothing_of_the_patch(session_factory, scope):
    sc, foreign_before = scope
    rid, held, added, patched, cancelled, inv = await _patch_versus_cancel(
        session_factory, sc, fetch_seconds=SLOW_SECONDS, flip_seconds=0, cancel_on=("fetch", 0)
    )
    assert isinstance(patched, ReservationStatusChanged)
    assert str(patched) == (
        "Reservation changed status during the update (ACTIVE to CANCELLED); nothing was changed"
    )
    assert cancelled.status == CANCELLED
    assert await _status_of(session_factory, rid) == CANCELLED
    async with session_factory() as s:
        row = (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()
    assert [str(d) for d in row.device_ids] == [str(held)]
    assert await _events_for(sc, rid, "herd.reservations.updated") == []
    assert len(await _events_for(sc, rid, "herd.reservations.cancelled")) == 1
    assert inv.last_write_for(added) is None
    assert inv.calls == [([str(held)], "AVAILABLE")]
    assert await sc.foreign_snapshot() == foreign_before


async def test_cancel_during_the_post_commit_flip_leaves_the_added_device_released(
    session_factory, scope
):
    sc, foreign_before = scope
    rid, held, added, patched, cancelled, inv = await _patch_versus_cancel(
        session_factory, sc, fetch_seconds=0, flip_seconds=SLOW_SECONDS, cancel_on=("flip", 0)
    )
    assert isinstance(patched, Reservation)
    assert cancelled.status == CANCELLED
    assert await _status_of(session_factory, rid) == CANCELLED
    assert len(await _events_for(sc, rid, "herd.reservations.updated")) == 1
    assert len(await _events_for(sc, rid, "herd.reservations.cancelled")) == 1
    # The flip landed, after the cancel's release, and only the revert follows it.
    assert ([str(added)], "RESERVED") in inv.calls
    assert inv.last_write_for(added) == "AVAILABLE"
    assert inv.last_write_for(held) == "AVAILABLE"
    assert await sc.foreign_snapshot() == foreign_before


async def test_patch_versus_cancel_jittered_around_the_patch_commit(session_factory, scope):
    """Fire the cancel at offsets straddling the PATCH's commit so the row lock is contended.

    The first offset makes the PATCH lose and the last fires well after the PATCH
    returned, so both branches run on every pass (issue #1136); the jittered ones
    between them contend for the row lock and land inside the post-commit flip.
    """
    sc, foreign_before = scope
    rng = random.Random(994)
    offsets = (
        [0.0]
        + [rng.uniform(SLOW_SECONDS - 0.05, SLOW_SECONDS + 0.05) for _ in range(7)]
        + [SLOW_SECONDS + 0.5]
    )
    outcomes = set()
    for offset in offsets:
        rid, held, added, patched, cancelled, inv = await _patch_versus_cancel(
            session_factory,
            sc,
            fetch_seconds=SLOW_SECONDS,
            flip_seconds=0.05,
            cancel_on=("fetch", offset),
        )
        assert cancelled.status == CANCELLED
        assert await _status_of(session_factory, rid) == CANCELLED, f"offset {offset}"
        assert len(await _events_for(sc, rid, "herd.reservations.cancelled")) == 1
        updated = await _events_for(sc, rid, "herd.reservations.updated")
        if isinstance(patched, ReservationStatusChanged):
            outcomes.add("patch_lost")
            assert updated == []
            assert inv.last_write_for(added) is None
        else:
            outcomes.add("patch_won")
            assert len(updated) == 1
            assert inv.last_write_for(added) == "AVAILABLE", f"offset {offset}"
        assert inv.last_write_for(held) == "AVAILABLE"
    assert outcomes == {"patch_lost", "patch_won"}
    assert await sc.foreign_snapshot() == foreign_before
