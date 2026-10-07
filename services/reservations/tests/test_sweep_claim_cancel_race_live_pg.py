"""Postgres-live proof that the sweep's PENDING claim is safe against a concurrent cancel.

Rule RES-STATUS-3, issue #998. Every reservation status write is a compare-and-swap
except one: the expiration sweep's claim of due PENDING rows into PENDING_PROVISION
(`_claim_due_pending_rows`), an ORM set on rows its query locked with
`SELECT ... FOR UPDATE SKIP LOCKED`. Its safety rests entirely on the Postgres row
lock, which SQLite never takes, so only a real Postgres can pin it. Both orderings run
as REAL concurrent asyncio tasks over SEPARATE sessions, through the production
functions:

  - the claim holds the row first: the cancel's compare-and-swap blocks on the lock,
    then finds PENDING_PROVISION, re-reads, and cancels from there, releasing the
    devices the claim's activation would have held; the activation that follows sees
    CANCELLED and does nothing;
  - the cancel holds the row first (paused after its compare-and-swap, before its
    commit): SKIP LOCKED leaves the row out, the claim claims nothing, and the cancel
    commits.

Asserted in both: the row ends CANCELLED (never a zombie PENDING_PROVISION), exactly one
reservation.cancelled is staged, and no reservation.created exists.

Env contract identical to test_reservation_status_cas_live_pg.py (HERD_TEST_PG_DSN,
HERD_TEST_PG_REQUIRED). Gate-ledger scoping (issue #819): the gate runs this against
its ALREADY-USED database, so the claim runs the REAL query (`_due_pending_stmt`)
narrowed to this test's own row, every OTHER reservation row is snapshotted before and
after and must be unchanged, and the test's rows and outbox events are deleted in a
finally. The gate stack runs a live reservations sweeper every few seconds, so each
row starts an hour ahead (never due on the real clock) and the test's claim passes a
`now` just past that start to the real query; the live sweeper never claims, fails, or
completes the row.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import app.services.reservation_service as svc_module
import pytest
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.services.reservation_service import cancel_reservation
from app.tasks.expiration import (
    _activate_pending_reservation,
    _claim_due_pending_rows,
    _due_pending_stmt,
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
EXP = "app.tasks.expiration"
PENDING = ReservationStatus.PENDING
CANCELLED = ReservationStatus.CANCELLED

# How long a blocked task is given to prove it is blocked.
BLOCK_SECONDS = 0.5
# How far ahead each row starts, so the gate's live sweeper never finds it due.
LEAD = timedelta(hours=1)


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def scope(session_factory):
    """A random owner; foreign rows snapshotted; own rows and events deleted after."""

    class Scope:
        user_id = uuid.uuid4()
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

        async def events(self, subject):
            mine = {str(i) for i in await self.my_ids()}
            async with session_factory() as s:
                rows = (
                    (
                        await s.execute(
                            select(OutboxEvent).where(
                                OutboxEvent.created_at >= self.started,
                                OutboxEvent.subject == subject,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            return [r for r in rows if r.payload.get("reservation_id") in mine]

    sc = Scope()
    before = await sc.foreign_snapshot()
    try:
        yield sc, before
    finally:
        mine = list(await sc.my_ids())
        async with session_factory() as s:
            if mine:
                await s.execute(
                    text("DELETE FROM reservation_devices WHERE reservation_id = ANY(:ids)"),
                    {"ids": mine},
                )
                await s.execute(
                    text("DELETE FROM outbox WHERE payload->>'reservation_id' = ANY(:ids)"),
                    {"ids": [str(i) for i in mine]},
                )
                await s.execute(
                    text("DELETE FROM reservations WHERE id = ANY(:ids)"), {"ids": mine}
                )
            await s.commit()


@pytest.fixture
def inventory():
    update = AsyncMock(side_effect=lambda ids, status, **_: list(ids))

    async def exclusive(ids):
        return [{"id": str(i), "exclusive": True} for i in ids]

    with (
        patch(f"{SVC}._update_device_statuses", new=update),
        patch(f"{EXP}._update_device_statuses", new=update),
        patch(f"{SVC}._fetch_devices_best_effort", new=exclusive),
        patch(f"{EXP}._fetch_devices_best_effort", new=exclusive),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{EXP}._create_reservation_fork_best_effort", new=AsyncMock()),
    ):
        yield update


async def _insert_due_pending(session_factory, sc) -> uuid.UUID:
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with session_factory() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=sc.user_id,
                device_ids=[str(uuid.uuid4())],
                topology_type=TopologyType.PHYSICAL,
                purpose="live pg sweep claim",
                start_time=now + LEAD,
                end_time=now + LEAD + timedelta(hours=1),
                status=PENDING,
            )
        )
        await s.commit()
    return rid


async def _status_of(session_factory, rid):
    async with session_factory() as s:
        return (
            await s.execute(select(Reservation.status).where(Reservation.id == rid))
        ).scalar_one()


def _claim_now() -> datetime:
    # Just past the rows' start: due for this test's claim, never for the live sweeper.
    return datetime.now(timezone.utc) + LEAD + timedelta(seconds=1)


def _own_due_stmt(rid):
    # The REAL claim query, narrowed to this test's row (never replaced).
    return _due_pending_stmt(_claim_now()).where(Reservation.id == rid)


async def test_claim_holds_the_row_first_cancel_blocks_then_cancels_from_provision(
    session_factory, scope, inventory
):
    sc, foreign_before = scope
    rid = await _insert_due_pending(session_factory, sc)

    selected = asyncio.Event()
    commit_claim = asyncio.Event()

    async def sweep_claim():
        async with session_factory() as db:
            # The claim's SELECT ... FOR UPDATE has run (the row is locked) but the
            # status change is not flushed yet: the lock alone must hold off the
            # cancel. Without FOR UPDATE the cancel would win here and the claim's
            # later UPDATE by primary key would overwrite CANCELLED.
            ids = await _claim_due_pending_rows(db, _own_due_stmt(rid), _claim_now())
            selected.set()
            await commit_claim.wait()
            await db.commit()
            return ids

    async def cancel():
        await selected.wait()
        async with session_factory() as db:
            return await cancel_reservation(db, rid, sc.user_id, "tok")

    claim_task = asyncio.create_task(sweep_claim())
    cancel_task = asyncio.create_task(cancel())
    try:
        await selected.wait()
        await asyncio.sleep(BLOCK_SECONDS)
        assert not cancel_task.done(), "the cancel did not wait on the claim's row lock"
    finally:
        commit_claim.set()
        claimed, cancelled = await asyncio.gather(claim_task, cancel_task)
    assert claimed == [rid], "precondition: the claim took this test's row"
    assert cancelled.status == CANCELLED

    # The activation the sweep runs next sees CANCELLED and does nothing.
    with patch(f"{EXP}.AsyncSessionLocal", new=session_factory):
        assert await _activate_pending_reservation(rid) is False

    assert await _status_of(session_factory, rid) == CANCELLED
    assert len(await sc.events("herd.reservations.cancelled")) == 1
    assert await sc.events("herd.reservations.created") == []
    # The cancel left PENDING_PROVISION, which holds devices, so it released them.
    assert [c.args[1] for c in inventory.await_args_list] == ["AVAILABLE"]
    assert await sc.foreign_snapshot() == foreign_before


async def test_cancel_holds_the_row_first_claim_skips_it(session_factory, scope, inventory):
    sc, foreign_before = scope
    rid = await _insert_due_pending(session_factory, sc)

    cancel_holding = asyncio.Event()
    let_cancel_commit = asyncio.Event()
    real_enqueue = svc_module.enqueue_event

    async def paused_enqueue(*args, **kwargs):
        # Called after the cancel's compare-and-swap UPDATE and before its commit,
        # so the cancel's transaction holds the row lock while it waits here.
        cancel_holding.set()
        await let_cancel_commit.wait()
        return await real_enqueue(*args, **kwargs)

    async def cancel():
        async with session_factory() as db:
            return await cancel_reservation(db, rid, sc.user_id, "tok")

    async def claim_and_commit():
        async with session_factory() as db:
            ids = await _claim_due_pending_rows(db, _own_due_stmt(rid), _claim_now())
            await db.commit()
            return ids

    with patch(f"{SVC}.enqueue_event", new=paused_enqueue):
        cancel_task = asyncio.create_task(cancel())
        try:
            await cancel_holding.wait()
            # SKIP LOCKED must leave the locked row out at once; a claim that waits
            # on the cancel's lock (or selects the row unlocked and then blocks on
            # its own UPDATE) times out here instead.
            claimed = await asyncio.wait_for(claim_and_commit(), timeout=5)
        finally:
            let_cancel_commit.set()
            cancelled = await cancel_task

    assert claimed == []
    assert cancelled.status == CANCELLED
    assert await _status_of(session_factory, rid) == CANCELLED
    assert len(await sc.events("herd.reservations.cancelled")) == 1
    assert await sc.events("herd.reservations.created") == []
    # A PENDING row holds nothing, so its cancel writes no inventory status.
    inventory.assert_not_called()
    assert await sc.foreign_snapshot() == foreign_before
