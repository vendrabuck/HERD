"""Postgres-live proof that reservation status transitions are compare-and-swaps.

Issue #899. `create_reservation` (immediate, physical-only path) committed
PENDING_PROVISION, spent seconds on inventory HTTP calls, then wrote ACTIVE with an
UPDATE keyed by id alone. A cancel that committed in that window was overwritten,
leaving a zombie ACTIVE reservation with both a terminal and a created event. The
auto-complete sweep versus a manual release had the same shape with a smaller window
and staged two terminal events. Every transition is now a conditional
`UPDATE ... WHERE id = :id AND status IN (:expected)` whose rowcount decides who
performs the side effects.

The SQLite unit suites (test_reservation_status_cas.py, test_expiration_status_cas.py)
pin each loser path deterministically, but SQLite never contends for a row. This file
runs the two races as REAL concurrent asyncio tasks over SEPARATE Postgres sessions,
through the real production functions and the real ORM models:

  - create_reservation versus cancel_reservation: the create's inventory flip is a
    slow stub, the cancel fires from a second session mid-flip (deterministic), and a
    jittered loop fires it at offsets straddling the flip's end so the row lock
    itself is contended;
  - the sweep's auto-complete step (`_complete_expired_rows`) versus
    release_reservation on the same ACTIVE row.

Asserted in every case: the row ends in exactly one terminal status, exactly one
terminal event is staged for it, a lost create stages no reservation.created and
makes no fork call, and no zombie ACTIVE survives a cancel that returned CANCELLED.

Env contract identical to services/cabling/tests/test_fork_restore_save_race_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database, so every test scopes to the reservations it created (a random user_id and
random device ids; nothing else references them), snapshots every OTHER reservation
row before and after and asserts nothing about them changed, and deletes its own rows
and outbox events in a finally. The sweep's whole-table select is deliberately NOT
run: `_complete_expired_rows` is called with this test's own row only. Rows are given
an end_time an hour ahead so a live reservations service's sweeper (the gate stack
runs one every few seconds) never touches them; the auto-complete step does not look
at the clock, it completes whatever rows it is handed.

The engine pins search_path to the reservations schema: the models are mapped
schema-less by the SQLite-oriented conftest, and `outbox` exists in several service
schemas, so the database-level search_path would be ambiguous.
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
from app.schemas.reservation import ReservationCreate
from app.services.reservation_service import (
    cancel_reservation,
    create_reservation,
    release_reservation,
)
from app.tasks.expiration import _complete_expired_rows
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
PENDING_PROVISION = ReservationStatus.PENDING_PROVISION
ACTIVE = ReservationStatus.ACTIVE
CANCELLED = ReservationStatus.CANCELLED
COMPLETED = ReservationStatus.COMPLETED

# Long enough that a task started mid-flip provably lands inside it, short enough to
# keep the suite quick.
FLIP_SECONDS = 0.4


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


def _devices(ids):
    return [
        {
            "id": str(i),
            "name": f"live-{str(i)[:6]}",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "exclusive": True,
        }
        for i in ids
    ]


async def _exclusive(ids):
    return [{"id": str(i), "exclusive": True} for i in ids]


class Inventory:
    """Slow fake inventory; `flipping` is set once the RESERVED flip has begun."""

    def __init__(self, flip_seconds=FLIP_SECONDS):
        self.calls: list[tuple[list[str], str]] = []
        self.flipping = asyncio.Event()
        self.flip_seconds = flip_seconds

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        self.calls.append(([str(i) for i in ids], status))
        if status == "RESERVED":
            self.flipping.set()
            await asyncio.sleep(self.flip_seconds)
            if succeeded is not None:
                succeeded.update(ids)
        return list(ids)


def _patches(inv, fork):
    async def fetch(ids, token):
        return _devices(ids)

    return (
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._fetch_devices_best_effort", new=_exclusive),
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._create_reservation_fork_best_effort", new=fork),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
    )


def _body(device_ids):
    now = datetime.now(timezone.utc)
    return ReservationCreate(
        device_ids=list(device_ids),
        purpose="live pg cas",
        start_time=now,
        end_time=now + timedelta(hours=1),
    )


async def _status_of(session_factory, rid):
    async with session_factory() as s:
        return (
            await s.execute(select(Reservation.status).where(Reservation.id == rid))
        ).scalar_one()


async def _one_create_versus_cancel(session_factory, sc, cancel_after: float | None):
    """Run create and cancel concurrently. cancel_after None means "as soon as flipping".

    Returns (final_status, cancel_result_status, inventory calls, fork mock).
    """
    inv = Inventory()
    fork = AsyncMock()
    devices = [uuid.uuid4()]
    p1, p2, p3, p4, p5 = _patches(inv, fork)

    async def do_create():
        async with session_factory() as db:
            return await create_reservation(db, _body(devices), sc.user_id, "tok")

    async def do_cancel():
        await inv.flipping.wait()
        if cancel_after:
            await asyncio.sleep(cancel_after)
        async with session_factory() as db:
            rid = (
                (
                    await db.execute(
                        select(Reservation.id)
                        .where(Reservation.user_id == sc.user_id)
                        .order_by(Reservation.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            return rid, await cancel_reservation(db, rid, sc.user_id, "tok")

    with p1, p2, p3, p4, p5:
        created, (rid, cancelled) = await asyncio.gather(do_create(), do_cancel())
    return rid, created, cancelled, inv, fork


async def test_create_versus_cancel_mid_flip_never_leaves_a_zombie_active(session_factory, scope):
    sc, foreign_before = scope
    rid, created, cancelled, inv, fork = await _one_create_versus_cancel(session_factory, sc, None)

    assert await _status_of(session_factory, rid) == CANCELLED
    assert created.status == CANCELLED
    assert cancelled.status == CANCELLED
    assert await sc.events("herd.reservations.created") == []
    assert await sc.events("herd.reservations.failed") == []
    assert len(await sc.events("herd.reservations.cancelled")) == 1
    fork.assert_not_called()
    # RESERVED was written by the create's flip; the loser reverted exactly those
    # devices (the cancel's own release may add an idempotent AVAILABLE too).
    assert inv.calls[0][1] == "RESERVED"
    assert all(status == "AVAILABLE" for _, status in inv.calls[1:])
    assert {d for ids, st in inv.calls[1:] for d in ids} == {str(d) for d in inv.calls[0][0]}
    assert await sc.foreign_snapshot() == foreign_before


async def test_create_versus_cancel_jittered_around_the_flip_end(session_factory, scope):
    """Fire the cancel at offsets straddling the create's commit so the row lock is contended."""
    sc, foreign_before = scope
    rng = random.Random(899)
    offsets = [None, 0.0] + [
        rng.uniform(FLIP_SECONDS - 0.05, FLIP_SECONDS + 0.05) for _ in range(6)
    ]
    for offset in offsets:
        # Fresh reservation ids per trial: clear this test's rows between trials.
        rid, created, cancelled, inv, fork = await _one_create_versus_cancel(
            session_factory, sc, offset
        )
        final = await _status_of(session_factory, rid)
        # The cancel succeeded, so the row is CANCELLED whichever side won the lock.
        assert final == CANCELLED, f"zombie {final} at offset {offset}"
        assert cancelled.status == CANCELLED
        created_events = [
            e
            for e in await sc.events("herd.reservations.created")
            if e.payload["reservation_id"] == str(rid)
        ]
        cancelled_events = [
            e
            for e in await sc.events("herd.reservations.cancelled")
            if e.payload["reservation_id"] == str(rid)
        ]
        assert len(cancelled_events) == 1
        assert len(created_events) <= 1
        if not created_events:
            fork.assert_not_called()
    assert await sc.foreign_snapshot() == foreign_before


async def test_auto_complete_versus_release_stages_exactly_one_terminal_event(
    session_factory, scope
):
    sc, foreign_before = scope
    rid = uuid.uuid4()
    async with session_factory() as s:
        row = Reservation(
            id=rid,
            user_id=sc.user_id,
            device_ids=[str(d) for d in sc.device_ids],
            topology_type=TopologyType.PHYSICAL,
            purpose="live pg cas",
            start_time=datetime.now(timezone.utc) - timedelta(hours=1),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
            status=ACTIVE,
        )
        s.add(row)
        await s.commit()

    inv = Inventory(flip_seconds=0)
    fork = AsyncMock()
    p1, p2, p3, p4, p5 = _patches(inv, fork)
    barrier = asyncio.Barrier(2)

    async def sweep():
        async with session_factory() as db:
            rows = (
                (await db.execute(select(Reservation).where(Reservation.id == rid))).scalars().all()
            )
            await barrier.wait()
            done = await _complete_expired_rows(db, rows)
            await db.commit()
            return done

    async def release():
        async with session_factory() as db:
            await barrier.wait()
            return await release_reservation(db, rid, sc.user_id, "tok")

    with p1, p2, p3, p4, p5:
        swept, released = await asyncio.gather(sweep(), release())

    assert await _status_of(session_factory, rid) == COMPLETED
    assert released.status == COMPLETED
    events = await sc.events("herd.reservations.completed")
    assert len(events) == 1
    # Exactly one writer owned the transition: either the sweep completed the row
    # or release did (in which case the sweep's list is empty), never both.
    release_won = len(swept) == 0
    sweep_won = len(swept) == 1
    assert release_won != sweep_won
    assert await sc.foreign_snapshot() == foreign_before
