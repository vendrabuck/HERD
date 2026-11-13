"""Postgres-live proof that a VLAN allocation is released by exactly one writer (#1357).

`_release_orphaned_allocations` used to read an ACTIVE allocation and set it RELEASED
with a bare ORM write, so two callers reaching one allocation at once (a reservation's
L2 apply tail and the retry channels' release of superseded rows) could both "win" and
both drive delete_vlan. The release is now one guarded UPDATE through
`vlan_service.release_allocation_if_orphaned`: it matches only while the row is ACTIVE
and no ACTIVE membership references it, and the row count decides the winner.

The SQLite unit tests (tests/test_vlan_service.py and
tests/test_nats_consumer_vlan_definitions.py) pin the predicate and the loser's
behavior, but one SQLite connection cannot make two writers contend for a row. Here
each releaser is a real asyncio task on its own Postgres connection:

  - two releases of one allocation are held at the row lock together (a third
    connection holds the row FOR UPDATE until both UPDATEs are seen waiting in
    pg_stat_activity), then let go: exactly one releases it, one set of delete_vlan
    calls is driven, the other logs the lost race;
  - a membership committed after a releaser's orphan check keeps the allocation
    ACTIVE and drives nothing;
  - a releaser that lost to an earlier release never touches the NEW allocation the
    same reservation was given for the same number and fabric in between.

Inventory, the template read, the driver load, the sandbox, and cabling's fabric
lookup are patched; everything from the ledger down is the real thing, including the
execution_runs rows the delete_vlan sessions write.

Used ledger (issue #819 and docs/specs/TESTING.md): this suite runs in the gate's
`_gate-pg-live-tests` phase against the gate's already-migrated, already-used
execution schema. Every test runs on random reservation ids, seeds FOREIGN rows under
other random reservations (an ACTIVE allocation holding the same VLAN number on
another fabric with an ACTIVE membership, a RELEASED allocation, and a RELEASED
membership row of another reservation pointing at the allocation under test), snapshots
every vlan_assignments and l2_port_assignments row it does not own before and after,
asserts nothing changed, and deletes its own and its seeded rows in a finally. The
fabric lookup answers a fresh fabric id for every switch it was not told about, so no
allocation already in the ledger can ever read as superseding.

Connections set search_path to the execution schema rather than flipping
`__table__.schema` (the reason is in test_wiring_retry_due_predicate_live_pg.py) and a
5 s lock_timeout, so a releaser that waits on a lock nobody releases fails in seconds.

Env contract identical to the other execution live-pg suites:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import defaultdict
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.models.execution_run import ExecutionRun
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.nats_consumer import _FetchContext, _release_orphaned_allocations
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")


async def _pg_reachable() -> bool:
    engine = create_async_engine(PG_DSN)
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


EXECUTION_SCHEMA = "execution"
LOCK_TIMEOUT = "5s"
LOST_RACE_ACTION = "vlan_release_lost_race"


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(
        PG_DSN,
        connect_args={
            "server_settings": {"search_path": EXECUTION_SCHEMA, "lock_timeout": LOCK_TIMEOUT}
        },
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


def _session_ctx(session_factory):
    """A get_db_session factory of the shape _release_orphaned_allocations expects:
    every session it opens checks out its own pooled connection."""

    class _Ctx:
        async def __aenter__(self):
            self._session = session_factory()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


DRIVER_ID = str(uuid.uuid4())
TEMPLATE_DATA = {"id": "tmpl-live", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}


def _switch(switch_id: str) -> dict:
    return {
        "id": switch_id,
        "name": "live-pg-l2",
        "template_id": "tmpl-live",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": "Layer 2 Switch",
        "field_data": {"ip": "10.0.0.1"},
    }


class _Harness:
    """Own and foreign ids for one test, the patched upstreams, and the call record."""

    def __init__(self):
        self.reservation = uuid.uuid4()
        self.foreign = uuid.uuid4()
        self.fabric = uuid.uuid4()
        self.switches = sorted(str(uuid.uuid4()) for _ in range(2))
        self.calls: list[tuple[str, str, int]] = []
        # Every switch not named here gets its own fresh fabric id, so no allocation
        # already in a used ledger can reach this test's component.
        self._fabrics: dict[str, uuid.UUID] = defaultdict(uuid.uuid4)
        for sid in self.switches:
            self._fabrics[sid] = self.fabric

    def _execute(self, driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        self.calls.append((action, context.get("HERD_device_id"), mk.get("vlan_id")))
        return SUCCESS_RESULT

    async def _fetch_fabric(self, device_id):
        return self._fabrics[str(device_id)]

    def patches(self, release=None):
        stack = ExitStack()
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                new=AsyncMock(side_effect=lambda did, client=None: _switch(str(did))),
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template",
                new=AsyncMock(return_value=TEMPLATE_DATA),
            )
        )
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/d"))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=self._execute)
        )
        stack.enter_context(
            patch(
                "app.services.vlan_service.fetch_fabric_id",
                new=AsyncMock(side_effect=self._fetch_fabric),
            )
        )
        if release is not None:
            stack.enter_context(
                patch("app.services.vlan_service.release_allocation_if_orphaned", new=release)
            )
        return stack

    def deletes(self) -> list[tuple[str, int]]:
        return sorted((sid, vlan) for action, sid, vlan in self.calls if action == "delete_vlan")


async def _free_vlan_number(session_factory) -> int:
    """A number between 2 and 4094 no ACTIVE row in the ledger holds, so a test's
    allocations never meet a same-number rival it did not seed."""
    async with session_factory() as s:
        held = set(
            (
                await s.execute(
                    select(VlanAssignment.vlan_id).where(VlanAssignment.status == "ACTIVE")
                )
            )
            .scalars()
            .all()
        )
    return next(n for n in range(4094, 1, -1) if n not in held)


async def _seed(session_factory, h: _Harness, vlan_id: int) -> uuid.UUID:
    """The allocation under test (ACTIVE, defined on both switches, no ACTIVE member)
    plus the foreign rows, all committed."""
    async with session_factory() as s:
        mine = VlanAssignment(
            reservation_id=h.reservation,
            fabric_id=h.fabric,
            vlan_id=vlan_id,
            switch_device_ids=list(h.switches),
            defined_switch_ids=list(h.switches),
            status="ACTIVE",
        )
        rival = VlanAssignment(
            reservation_id=h.foreign,
            fabric_id=uuid.uuid4(),
            vlan_id=vlan_id,
            switch_device_ids=[str(uuid.uuid4())],
            defined_switch_ids=[],
            status="ACTIVE",
        )
        old = VlanAssignment(
            reservation_id=h.foreign,
            fabric_id=h.fabric,
            vlan_id=vlan_id,
            switch_device_ids=list(h.switches),
            defined_switch_ids=[],
            status="RELEASED",
        )
        s.add_all([mine, rival, old])
        await s.flush()
        s.add_all(
            [
                L2PortAssignment(
                    reservation_id=h.foreign,
                    vlan_assignment_id=rival.id,
                    switch_device_id=uuid.UUID(rival.switch_device_ids[0]),
                    port="0/0/1",
                    intended="ACTIVE",
                    status="ACTIVE",
                ),
                # History of another reservation pointing at the allocation under
                # test: not ACTIVE, so it must neither hold the allocation nor change.
                L2PortAssignment(
                    reservation_id=h.foreign,
                    vlan_assignment_id=mine.id,
                    switch_device_id=uuid.UUID(h.switches[0]),
                    port="0/0/2",
                    intended="RELEASED",
                    status="RELEASED",
                ),
            ]
        )
        await s.commit()
        return mine.id


async def _foreign_snapshot(session_factory, h: _Harness):
    """Every vlan_assignments and l2_port_assignments row this test does not own,
    including the foreign rows it seeded."""
    async with session_factory() as s:
        allocations = (
            await s.execute(
                select(
                    VlanAssignment.id,
                    VlanAssignment.reservation_id,
                    VlanAssignment.status,
                    VlanAssignment.vlan_id,
                    VlanAssignment.released_at,
                    VlanAssignment.switch_device_ids,
                    VlanAssignment.defined_switch_ids,
                ).where(VlanAssignment.reservation_id != h.reservation)
            )
        ).all()
        memberships = (
            await s.execute(
                select(
                    L2PortAssignment.id,
                    L2PortAssignment.reservation_id,
                    L2PortAssignment.vlan_assignment_id,
                    L2PortAssignment.status,
                    L2PortAssignment.intended,
                    L2PortAssignment.released_at,
                    L2PortAssignment.claimed_until,
                ).where(L2PortAssignment.reservation_id != h.reservation)
            )
        ).all()
    return sorted(map(tuple, allocations), key=str), sorted(map(tuple, memberships), key=str)


async def _own_allocations(session_factory, h: _Harness) -> dict:
    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(VlanAssignment).where(VlanAssignment.reservation_id == h.reservation)
                )
            )
            .scalars()
            .all()
        )
    return {r.id: r for r in rows}


async def _own_delete_runs(session_factory, h: _Harness) -> int:
    async with session_factory() as s:
        return len(
            (
                await s.execute(
                    select(ExecutionRun.id).where(
                        ExecutionRun.reservation_id == h.reservation,
                        ExecutionRun.action == "delete_vlan",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _cleanup(session_factory, h: _Harness):
    ids = [h.reservation, h.foreign]
    async with session_factory() as s:
        await s.execute(delete(L2PortAssignment).where(L2PortAssignment.reservation_id.in_(ids)))
        await s.execute(delete(VlanAssignment).where(VlanAssignment.reservation_id.in_(ids)))
        await s.execute(delete(ExecutionRun).where(ExecutionRun.reservation_id.in_(ids)))
        await s.commit()


async def _waiting_on_lock(pg_engine, needle: str) -> int:
    async with pg_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database() "
                    "AND query ILIKE :needle"
                ),
                {"needle": needle},
            )
        ).scalar_one()


def _lost_race_records(caplog):
    return [r for r in caplog.records if getattr(r, "action", None) == LOST_RACE_ACTION]


async def test_two_concurrent_releases_of_one_allocation_have_exactly_one_winner(
    pg_engine, session_factory, caplog
):
    h = _Harness()
    vlan_id = await _free_vlan_number(session_factory)
    try:
        alloc = await _seed(session_factory, h, vlan_id)
        before = await _foreign_snapshot(session_factory, h)
        real = vlan_service.release_allocation_if_orphaned
        outcomes: list[bool] = []

        async def _recorded(db, allocation_id):
            won = await real(db, allocation_id)
            outcomes.append(won)
            return won

        get_db = _session_ctx(session_factory)
        with h.patches(release=_recorded), caplog.at_level(logging.INFO):
            async with pg_engine.connect() as holder:
                # Hold the row so both guarded UPDATEs queue on its lock together.
                await holder.execute(
                    select(VlanAssignment.id).where(VlanAssignment.id == alloc).with_for_update()
                )
                tasks = [
                    asyncio.create_task(
                        _release_orphaned_allocations([alloc], get_db, _FetchContext(None))
                    )
                    for _ in range(2)
                ]
                for _ in range(200):
                    if await _waiting_on_lock(pg_engine, "UPDATE vlan_assignments%") == 2:
                        break
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail("both releasers never queued on the allocation's row lock")
                await holder.rollback()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

        assert sorted(outcomes) == [False, True], "exactly one writer wins the swap"
        assert h.deletes() == sorted((sid, vlan_id) for sid in h.switches), (
            "one set of delete_vlan calls, not two"
        )
        assert await _own_delete_runs(session_factory, h) == len(h.switches)
        row = (await _own_allocations(session_factory, h))[alloc]
        assert (row.status, row.defined_switch_ids) == ("RELEASED", [])
        assert row.released_at is not None
        assert len(_lost_race_records(caplog)) == 1
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)


async def test_a_join_committed_after_the_orphan_check_keeps_the_allocation_active(
    session_factory, caplog
):
    h = _Harness()
    vlan_id = await _free_vlan_number(session_factory)
    try:
        alloc = await _seed(session_factory, h, vlan_id)
        before = await _foreign_snapshot(session_factory, h)
        real = vlan_service.release_allocation_if_orphaned
        arrived, proceed = asyncio.Event(), asyncio.Event()
        outcomes: list[bool] = []

        async def _held(db, allocation_id):
            arrived.set()
            await proceed.wait()
            won = await real(db, allocation_id)
            outcomes.append(won)
            return won

        with h.patches(release=_held), caplog.at_level(logging.INFO):
            task = asyncio.create_task(
                _release_orphaned_allocations(
                    [alloc], _session_ctx(session_factory), _FetchContext(None)
                )
            )
            await asyncio.wait_for(arrived.wait(), timeout=10)
            async with session_factory() as s:
                s.add(
                    L2PortAssignment(
                        reservation_id=h.reservation,
                        vlan_assignment_id=alloc,
                        switch_device_id=uuid.UUID(h.switches[0]),
                        port="0/0/9",
                        intended="ACTIVE",
                        status="ACTIVE",
                    )
                )
                await s.commit()
            proceed.set()
            await asyncio.wait_for(task, timeout=30)

        assert outcomes == [False]
        assert h.calls == []
        row = (await _own_allocations(session_factory, h))[alloc]
        assert (row.status, row.released_at, row.defined_switch_ids) == (
            "ACTIVE",
            None,
            list(h.switches),
        )
        assert len(_lost_race_records(caplog)) == 1
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)


async def test_the_loser_never_touches_a_new_allocation_of_the_same_number(session_factory, caplog):
    h = _Harness()
    vlan_id = await _free_vlan_number(session_factory)
    try:
        alloc = await _seed(session_factory, h, vlan_id)
        before = await _foreign_snapshot(session_factory, h)
        real = vlan_service.release_allocation_if_orphaned
        arrived, proceed = asyncio.Event(), asyncio.Event()
        outcomes: list[bool] = []

        async def _loser_held(db, allocation_id):
            if not arrived.is_set():
                arrived.set()
                await proceed.wait()
            won = await real(db, allocation_id)
            outcomes.append(won)
            return won

        get_db = _session_ctx(session_factory)
        with h.patches(release=_loser_held), caplog.at_level(logging.INFO):
            loser = asyncio.create_task(
                _release_orphaned_allocations([alloc], get_db, _FetchContext(None))
            )
            await asyncio.wait_for(arrived.wait(), timeout=10)
            # The winner: a whole release pass while the loser sits past its check.
            await _release_orphaned_allocations([alloc], get_db, _FetchContext(None))
            winner_deletes = h.deletes()
            # The same reservation is given the same number on the same fabric again.
            async with session_factory() as s:
                fresh = VlanAssignment(
                    reservation_id=h.reservation,
                    fabric_id=h.fabric,
                    vlan_id=vlan_id,
                    switch_device_ids=list(h.switches),
                    defined_switch_ids=list(h.switches),
                    status="ACTIVE",
                )
                s.add(fresh)
                await s.commit()
                fresh_id = fresh.id
            proceed.set()
            await asyncio.wait_for(loser, timeout=30)

        assert outcomes == [True, False]
        assert winner_deletes == sorted((sid, vlan_id) for sid in h.switches)
        assert h.deletes() == winner_deletes, "the loser drives no delete_vlan"
        rows = await _own_allocations(session_factory, h)
        assert rows[alloc].status == "RELEASED"
        assert (rows[fresh_id].status, rows[fresh_id].released_at) == ("ACTIVE", None)
        assert rows[fresh_id].defined_switch_ids == list(h.switches)
        assert len(_lost_race_records(caplog)) == 1
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)
