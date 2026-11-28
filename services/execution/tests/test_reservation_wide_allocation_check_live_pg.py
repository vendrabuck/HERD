"""Postgres-live proof of the reservation-wide allocation check against a concurrent
join (issue #1497, ADR 0018 D11, WIRE-VLAN-4).

`_release_reservation_orphaned_allocations` reads its candidates (the reservation's
ACTIVE allocations with no ACTIVE member that `held_by_a_failed_membership` does not
hold) and releases each through `release_allocation_if_orphaned(...,
reservation_wide=True)`, which locks the allocation row FOR UPDATE (issue #1413) and
then re-checks both the membership and the failed-membership condition in one guarded
UPDATE. The SQLite suite (tests/test_reservation_wide_allocation_check.py) pins the
predicate on one connection. Here the check runs on its own Postgres connections while
another connection commits or drives a join of the same reservation:

  - a FAILED intended-ACTIVE join (nil allocation, the WIRE-RETRY-8 shape) committed
    after the candidate read and before the UPDATE keeps every allocation of the
    reservation ACTIVE, drives no delete_vlan, and logs the lost race;
  - a join that flips to an ACTIVE membership of the allocation in that window keeps
    it ACTIVE the same way;
  - the frozen fact and the failed joins are read for the allocation's OWN reservation:
    another reservation that is frozen and has a FAILED join changes nothing, and a
    frozen reservation's own orphan is released;
  - a failed join committed while the release waits on the allocation's row lock is
    seen, since the lock is taken before the UPDATE's snapshot (#1413);
  - review finding 1's three interleavings, in which the check releases an allocation
    a join it cannot see is about to record (a first join in flight on another
    replica, in both orders; a retried nil join whose port a save removes; a teardown
    against a reconcile whose first join is in flight): the join loses at #1413's
    guard, with no ACTIVE membership on a RELEASED allocation, and its add is undone in
    the same pass or, on the frozen reservation, parked for removal and removed by the
    next retry.

Inventory, the template read, the driver load, the sandbox, and cabling's fabric lookup
are patched; everything from the ledger down is real.

Used ledger (issue #819 and docs/specs/TESTING.md): every test runs on random
reservation ids, seeds FOREIGN rows under other random reservations (one frozen with a
FAILED join and an orphaned ACTIVE allocation, one not frozen with an ACTIVE
allocation holding a member), snapshots every vlan_assignments, l2_port_assignments,
and reservation_wiring_state row it does not own before and after and asserts nothing
changed, seeds every FAILED row at 1,000,000 attempts and raises every FAILED row of
its own reservation to that count as soon as a pass under test has written it (so the
gate stack's tick, capped far lower, does not select it), and deletes its own and its
seeded rows in a finally. The fabric lookup answers a fresh fabric id for every switch,
so no allocation already in the ledger can read as superseding.

Env contract identical to the other execution live-pg suites:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN of the database to use. Unset or
                            empty, the suite is skipped and connects to nothing; a
                            named database that does not answer skips it too.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns either skip
                            into an error at setup (herd_common.pg_live_suite).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import defaultdict
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.models.execution_run import ExecutionRun
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.nats_consumer import _FetchContext, _release_reservation_orphaned_allocations
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


EXECUTION_SCHEMA = "execution"
LOCK_TIMEOUT = "5s"
COORDINATION_TIMEOUT = 10
# Far above the default retry cap of 10: hides a FAILED row from the gate stack's tick.
HIDDEN_ATTEMPTS = 1_000_000
LOST_RACE_ACTION = "vlan_release_lost_race"
NIL = uuid.UUID(int=0)


def _engine(application_name: str | None = None):
    settings = {"search_path": EXECUTION_SCHEMA, "lock_timeout": LOCK_TIMEOUT}
    if application_name:
        settings["application_name"] = application_name
    return create_async_engine(PG_DSN, connect_args={"server_settings": settings})


@pytest.fixture
async def pg_engine():
    engine = _engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


def _session_ctx(session_factory):
    class _Ctx:
        async def __aenter__(self):
            self._session = session_factory()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


DRIVER_ID = str(uuid.uuid4())
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
    """Own and foreign ids for one test, the patched upstreams, and the call record.

    `block` names one driver action that blocks in its worker thread until `unblock`
    is set; `blocked` is set when it starts. `wires` is what the fork read answers."""

    def __init__(self):
        self.reservation = uuid.uuid4()
        self.foreign_frozen = uuid.uuid4()
        self.foreign_live = uuid.uuid4()
        self.switch = str(uuid.uuid4())
        self.dut = str(uuid.uuid4())
        self.calls: list[tuple[str, str, int]] = []
        self._lock = threading.Lock()
        self.block: str | None = None
        self.blocked = threading.Event()
        self.unblock = threading.Event()
        self.wires: list[dict] = []
        self._fabrics: dict[str, uuid.UUID] = defaultdict(uuid.uuid4)

    @property
    def ids(self):
        return [self.reservation, self.foreign_frozen, self.foreign_live]

    def wire(self, port: str) -> dict:
        return {
            "device_a_id": self.dut,
            "port_a": "eth0",
            "device_b_id": self.switch,
            "port_b": port,
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": None,
        }

    def _execute(self, driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        with self._lock:
            self.calls.append((action, context.get("HERD_device_id"), mk.get("vlan_id")))
        if action == self.block:
            self.blocked.set()
            self.unblock.wait(COORDINATION_TIMEOUT)
        return SUCCESS_RESULT

    async def _fetch_device(self, device_id, client=None):
        did = str(device_id)
        device = _switch(did)
        if did == self.dut:
            device["connection_type"] = "Server"
        return device

    async def _fetch_wires(self, reservation_id, client=None):
        return list(self.wires)

    async def _fetch_fabric(self, device_id):
        return self._fabrics[str(device_id)]

    def patches(self, release=None):
        stack = ExitStack()
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                new=AsyncMock(side_effect=self._fetch_device),
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(side_effect=self._fetch_wires),
            )
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template",
                new=AsyncMock(return_value={"id": "tmpl-live", "name": "T", "sections": []}),
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

    def actions(self) -> list[str]:
        return [action for action, _sid, _vlan in self.calls if action not in ("login", "logout")]


async def _free_vlan_numbers(session_factory, count: int) -> list[int]:
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
    return [n for n in range(4094, 1, -1) if n not in held][:count]


async def _freeze(session_factory, reservation_id) -> None:
    from app.services.l1_assignment_service import freeze_reservation_wiring

    async with session_factory() as s:
        await freeze_reservation_wiring(s, reservation_id)


def _membership(reservation, allocation, switch, port, *, status="ACTIVE", intended="ACTIVE"):
    return L2PortAssignment(
        reservation_id=reservation,
        vlan_assignment_id=allocation,
        switch_device_id=uuid.UUID(switch),
        port=port,
        intended=intended,
        status=status,
        attempts=HIDDEN_ATTEMPTS if status == "FAILED" else 0,
        last_error="driver timeout" if status == "FAILED" else None,
    )


async def _seed(
    session_factory, h: _Harness, *, frozen: bool = False, own: bool = True
) -> uuid.UUID | None:
    """The allocation under test (ACTIVE, defined on one switch, no member) of
    h.reservation, plus the foreign rows; returns the allocation id. With `own` False
    only the foreign rows are written (the pass under test makes its own allocation)."""
    mine_vlan, foreign_vlan, live_vlan = await _free_vlan_numbers(session_factory, 3)
    foreign_switch = str(uuid.uuid4())
    async with session_factory() as s:
        mine = VlanAssignment(
            reservation_id=h.reservation,
            fabric_id=uuid.uuid4(),
            vlan_id=mine_vlan,
            switch_device_ids=[h.switch],
            defined_switch_ids=[h.switch],
            status="ACTIVE",
        )
        foreign_orphan = VlanAssignment(
            reservation_id=h.foreign_frozen,
            fabric_id=uuid.uuid4(),
            vlan_id=foreign_vlan,
            switch_device_ids=[foreign_switch],
            defined_switch_ids=[],
            status="ACTIVE",
        )
        foreign_held = VlanAssignment(
            reservation_id=h.foreign_live,
            fabric_id=uuid.uuid4(),
            vlan_id=live_vlan,
            switch_device_ids=[foreign_switch],
            defined_switch_ids=[],
            status="ACTIVE",
        )
        s.add_all([mine, foreign_orphan, foreign_held] if own else [foreign_orphan, foreign_held])
        await s.flush()
        s.add_all(
            [
                _membership(
                    h.foreign_frozen, foreign_orphan.id, foreign_switch, "0/0/1", status="FAILED"
                ),
                _membership(h.foreign_live, foreign_held.id, foreign_switch, "0/0/2"),
            ]
        )
        if own:
            # History of another reservation pointing at the allocation under test.
            s.add(
                _membership(
                    h.foreign_live,
                    mine.id,
                    h.switch,
                    "0/0/3",
                    status="RELEASED",
                    intended="RELEASED",
                )
            )
        await s.commit()
        mine_id = mine.id if own else None
    await _freeze(session_factory, h.foreign_frozen)
    if frozen:
        await _freeze(session_factory, h.reservation)
    return mine_id


async def _foreign_snapshot(session_factory, h: _Harness):
    """Every vlan_assignments, l2_port_assignments, and reservation_wiring_state row
    this test does not own, including the foreign rows it seeded."""
    out = []
    async with session_factory() as s:
        for model in (VlanAssignment, L2PortAssignment, ReservationWiringState):
            cols = [c for c in model.__table__.columns]
            rows = (
                await s.execute(select(*cols).where(model.reservation_id != h.reservation))
            ).all()
            out.append(sorted(map(tuple, rows), key=str))
    return out


async def _allocation(session_factory, alloc_id) -> VlanAssignment:
    async with session_factory() as s:
        return (
            await s.execute(select(VlanAssignment).where(VlanAssignment.id == alloc_id))
        ).scalar_one()


async def _cleanup(session_factory, h: _Harness):
    async with session_factory() as s:
        await s.execute(delete(L2PortAssignment).where(L2PortAssignment.reservation_id.in_(h.ids)))
        await s.execute(delete(VlanAssignment).where(VlanAssignment.reservation_id.in_(h.ids)))
        await s.execute(delete(ExecutionRun).where(ExecutionRun.reservation_id.in_(h.ids)))
        await s.execute(
            delete(ReservationWiringState).where(ReservationWiringState.reservation_id.in_(h.ids))
        )
        await s.commit()


def _lost_race_records(caplog):
    return [r for r in caplog.records if getattr(r, "action", None) == LOST_RACE_ACTION]


async def _check(h: _Harness, get_db):
    await _release_reservation_orphaned_allocations(str(h.reservation), _FetchContext(None), get_db)


@pytest.mark.parametrize("join", ["failed_nil", "failed_real", "active"])
async def test_a_join_committed_between_the_candidate_read_and_the_update_keeps_the_allocation(
    session_factory, caplog, join
):
    h = _Harness()
    try:
        alloc = await _seed(session_factory, h)
        before = await _foreign_snapshot(session_factory, h)
        real = vlan_service.release_allocation_if_orphaned
        arrived, proceed = asyncio.Event(), asyncio.Event()
        outcomes: list[bool] = []

        async def _held(db, allocation_id, **kwargs):
            assert kwargs == {"reservation_wide": True}
            arrived.set()
            await proceed.wait()
            won = await real(db, allocation_id, **kwargs)
            outcomes.append(won)
            return won

        with h.patches(release=_held), caplog.at_level(logging.INFO):
            task = asyncio.create_task(_check(h, _session_ctx(session_factory)))
            await asyncio.wait_for(arrived.wait(), timeout=10)
            async with session_factory() as s:
                if join == "active":
                    s.add(_membership(h.reservation, alloc, h.switch, "0/0/9"))
                else:
                    target = NIL if join == "failed_nil" else alloc
                    s.add(_membership(h.reservation, target, h.switch, "0/0/9", status="FAILED"))
                await s.commit()
            proceed.set()
            await asyncio.wait_for(task, timeout=30)

        assert outcomes == [False]
        assert h.calls == []
        row = await _allocation(session_factory, alloc)
        assert (row.status, row.released_at, row.defined_switch_ids) == ("ACTIVE", None, [h.switch])
        assert len(_lost_race_records(caplog)) == 1
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)


@pytest.mark.parametrize("frozen", [True, False])
async def test_the_check_reads_its_own_reservation_on_a_used_ledger(session_factory, frozen):
    """With no join of its own, the reservation's orphan is released (and its VLAN
    deleted) whether or not it is frozen; another reservation's freeze, its FAILED join,
    and its orphan are neither read for this one nor touched."""
    h = _Harness()
    try:
        alloc = await _seed(session_factory, h, frozen=frozen)
        before = await _foreign_snapshot(session_factory, h)
        with h.patches():
            await _check(h, _session_ctx(session_factory))
        row = await _allocation(session_factory, alloc)
        assert (row.status, row.defined_switch_ids) == ("RELEASED", [])
        assert h.deletes() == [(h.switch, row.vlan_id)]
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)


async def test_a_failed_join_of_a_frozen_reservation_does_not_hold_and_of_a_live_one_does(
    session_factory,
):
    """The two branches of the condition on Postgres, in one used ledger: the same
    FAILED nil-allocation join keeps the allocation while the reservation is live, and
    no longer once it is frozen (no live claim)."""
    h = _Harness()
    try:
        alloc = await _seed(session_factory, h)
        async with session_factory() as s:
            s.add(_membership(h.reservation, NIL, h.switch, "0/0/9", status="FAILED"))
            await s.commit()
        before = await _foreign_snapshot(session_factory, h)
        with h.patches():
            await _check(h, _session_ctx(session_factory))
        assert (await _allocation(session_factory, alloc)).status == "ACTIVE"
        assert h.calls == []

        await _freeze(session_factory, h.reservation)
        with h.patches():
            await _check(h, _session_ctx(session_factory))
        assert (await _allocation(session_factory, alloc)).status == "RELEASED"
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await _cleanup(session_factory, h)


async def _waiting_on_lock(pg_engine, application_name: str) -> int:
    async with pg_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database() "
                    "AND application_name = :app"
                ),
                {"app": application_name},
            )
        ).scalar_one()


async def _seen_waiting(pg_engine, application_name: str, task: asyncio.Task) -> bool:
    """True once the party tagged `application_name` waits on a lock; False when its
    task finished without ever being seen waiting. Bounded."""
    for _ in range(int(COORDINATION_TIMEOUT / 0.02)):
        if await _waiting_on_lock(pg_engine, application_name) >= 1:
            return True
        if task.done():
            return False
        await asyncio.sleep(0.02)
    pytest.fail(f"{application_name} neither waited on a lock nor finished")


def _tagged_engine(role: str):
    name = f"herd-test-1497-{role}-{uuid.uuid4().hex[:10]}"
    return _engine(name), name


class _CommitGate:
    """Stops the FIRST commit of the sessions a gated maker opens, after a flush and
    before the commit, so the holder is inside its transaction with its locks held."""

    def __init__(self):
        self.inside = asyncio.Event()
        self.proceed = asyncio.Event()
        self.used = False
        self.returned = False


def _gated_maker(engine, gate: _CommitGate):
    class _GatedSession(AsyncSession):
        async def commit(self):
            if gate.used:
                return await super().commit()
            gate.used = True
            await self.flush()
            gate.inside.set()
            await asyncio.wait_for(gate.proceed.wait(), COORDINATION_TIMEOUT)
            await super().commit()
            gate.returned = True

    return async_sessionmaker(engine, class_=_GatedSession, expire_on_commit=False)


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


async def _own_memberships(session_factory, h: _Harness) -> list:
    async with session_factory() as s:
        return list(
            (
                await s.execute(
                    select(L2PortAssignment).where(L2PortAssignment.reservation_id == h.reservation)
                )
            )
            .scalars()
            .all()
        )


async def _hide_failed(session_factory, h: _Harness) -> None:
    """Raise every own FAILED row to HIDDEN_ATTEMPTS at once, so the gate stack's tick
    does not select a row a pass under test just wrote with a low count."""
    async with session_factory() as s:
        await s.execute(
            update(L2PortAssignment)
            .where(
                L2PortAssignment.reservation_id == h.reservation,
                L2PortAssignment.status == "FAILED",
            )
            .values(attempts=HIDDEN_ATTEMPTS)
        )
        await s.commit()


async def _assert_invariants(session_factory, h: _Harness, before) -> None:
    """No own ACTIVE membership on an allocation that is not ACTIVE; every create_vlan
    is followed by a delete_vlan of the same switch and number, or the switch is listed
    on an ACTIVE own allocation holding that number; foreign rows unchanged."""
    allocations = await _own_allocations(session_factory, h)
    for m in await _own_memberships(session_factory, h):
        if m.status == "ACTIVE":
            alloc = allocations.get(m.vlan_assignment_id)
            assert alloc is not None and alloc.status == "ACTIVE", (
                f"ACTIVE membership {m.id} on an allocation that is not ACTIVE"
            )
    for index, (action, switch, vlan) in enumerate(h.calls):
        if action != "create_vlan":
            continue
        deleted_later = ("delete_vlan", switch, vlan) in h.calls[index + 1 :]
        still_listed = any(
            a.status == "ACTIVE" and a.vlan_id == vlan and switch in a.defined_switch_ids
            for a in allocations.values()
        )
        assert deleted_later or still_listed, f"orphan definition of VLAN {vlan} on {switch}"
    assert await _foreign_snapshot(session_factory, h) == before


async def test_a_failed_join_committed_while_the_release_waits_on_the_row_lock_is_seen(
    pg_engine, session_factory, caplog
):
    """The reservation-wide release takes the allocation's row lock FOR UPDATE in its
    own statement before its guarded UPDATE (release_allocation_if_orphaned, issue
    #1413), as every release does. A failed join of the reservation that commits while
    the release waits on that lock (held here by a third connection) is therefore in
    the UPDATE's snapshot: the allocation is kept and the race is logged as lost. Before
    #1413 the wait was inside the UPDATE, whose snapshot predated the join, and the
    allocation was released."""
    h = _Harness()
    checker_engine = None
    try:
        alloc = await _seed(session_factory, h)
        before = await _foreign_snapshot(session_factory, h)
        checker_engine, app_name = _tagged_engine("check")
        get_db = _session_ctx(async_sessionmaker(checker_engine, expire_on_commit=False))
        with h.patches(), caplog.at_level(logging.INFO):
            async with pg_engine.connect() as holder:
                await holder.execute(
                    select(VlanAssignment.id).where(VlanAssignment.id == alloc).with_for_update()
                )
                task = asyncio.create_task(_check(h, get_db))
                assert await _seen_waiting(pg_engine, app_name, task), (
                    "the release must wait on the allocation's row lock"
                )
                async with session_factory() as s:
                    s.add(_membership(h.reservation, NIL, h.switch, "0/0/9", status="FAILED"))
                    await s.commit()
                await holder.rollback()
            await asyncio.wait_for(task, timeout=30)
        row = await _allocation(session_factory, alloc)
        assert (row.status, row.defined_switch_ids) == ("ACTIVE", [h.switch])
        assert h.calls == []
        assert len(_lost_race_records(caplog)) == 1
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        if checker_engine is not None:
            await checker_engine.dispose()
        await _cleanup(session_factory, h)


# --- review finding 1: the three interleavings, against the merged #1413 guard ---
#
# In each, the reservation-wide check releases an allocation a join of the same
# reservation is about to record, a join it cannot see (no FAILED row names it at the
# check). The join must lose cleanly at #1413's guard: no ACTIVE membership on a
# RELEASED allocation, and its add undone in the same pass (or, on a frozen
# reservation, parked for removal and removed by the next retry).


async def test_a_first_join_in_flight_loses_to_the_check_and_is_undone(session_factory):
    """Two consumer replicas. Replica 1 is driving the first join of the reservation on
    allocation A (its add_to_vlan is in flight; no membership row exists yet). Replica
    2's reservation-wide check sees A memberless with no failed join, releases it, and
    deletes its VLAN. Replica 1's join then finds A RELEASED at its share lock: it
    records no ACTIVE membership, removes the port from the number its add used (the
    undo, after the delete), and fails its row for re-allocation."""
    from app.services.l2_membership_service import JOIN_LOST_ALLOCATION_RELEASED
    from app.services.nats_consumer import _apply_l2_memberships

    h = _Harness()
    try:
        alloc = await _seed(session_factory, h)
        vlan = (await _allocation(session_factory, alloc)).vlan_id
        before = await _foreign_snapshot(session_factory, h)
        replica_1, _ = _tagged_engine("replica-1")
        replica_2, _ = _tagged_engine("replica-2")
        add = {
            "switch_device_id": h.switch,
            "port": "0/0/1",
            "vlan_assignment_id": alloc,
            "vlan_id": vlan,
        }
        h.block = "add_to_vlan"
        try:
            with h.patches():
                join = asyncio.create_task(
                    _apply_l2_memberships(
                        str(h.reservation),
                        [],
                        [add],
                        _FetchContext(None),
                        _session_ctx(async_sessionmaker(replica_1, expire_on_commit=False)),
                    )
                )
                assert await asyncio.to_thread(h.blocked.wait, COORDINATION_TIMEOUT)
                await _check(h, _session_ctx(async_sessionmaker(replica_2, expire_on_commit=False)))
                released = (await _allocation(session_factory, alloc)).status
                h.unblock.set()
                await asyncio.wait_for(join, COORDINATION_TIMEOUT)
        finally:
            h.unblock.set()
            await replica_1.dispose()
            await replica_2.dispose()
        await _hide_failed(session_factory, h)

        assert released == "RELEASED", "the check released A while the join was in flight"
        assert h.actions() == ["add_to_vlan", "delete_vlan", "remove_from_vlan"]
        (row,) = await _own_memberships(session_factory, h)
        assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
            "FAILED",
            "ACTIVE",
            alloc,
            JOIN_LOST_ALLOCATION_RELEASED,
        )
        await _assert_invariants(session_factory, h, before)
    finally:
        await _cleanup(session_factory, h)


async def test_a_first_join_holding_its_lock_makes_the_check_wait_and_lose(
    pg_engine, session_factory, caplog
):
    """The other order of the same two replicas: the join holds A FOR SHARE with its
    ACTIVE membership sent and uncommitted when the check reads its candidates (it
    reads A as memberless). The release waits on the join's lock, and once the join
    commits its UPDATE sees the member and matches nothing: A stays ACTIVE with its
    definition and no delete_vlan."""
    from app.services.l2_membership_service import record_l2_membership_active

    h = _Harness()
    join_engine = check_engine = None
    try:
        alloc = await _seed(session_factory, h)
        before = await _foreign_snapshot(session_factory, h)
        gate = _CommitGate()
        join_engine, _ = _tagged_engine("join")
        check_engine, check_app = _tagged_engine("check")
        with h.patches(), caplog.at_level(logging.INFO):
            join_session = _gated_maker(join_engine, gate)()
            try:
                join = asyncio.create_task(
                    record_l2_membership_active(
                        join_session, h.reservation, alloc, uuid.UUID(h.switch), "0/0/1"
                    )
                )
                await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)
                check = asyncio.create_task(
                    _check(
                        h, _session_ctx(async_sessionmaker(check_engine, expire_on_commit=False))
                    )
                )
                waited = await _seen_waiting(pg_engine, check_app, check)
                join_was_holding = not gate.returned
                gate.proceed.set()
                row = await asyncio.wait_for(join, COORDINATION_TIMEOUT)
                await asyncio.wait_for(check, COORDINATION_TIMEOUT)
            finally:
                gate.proceed.set()
                await join_session.close()

        assert join_was_holding
        assert waited, "the check's release must wait on the join's share lock"
        assert (row.status, row.vlan_assignment_id) == ("ACTIVE", alloc)
        allocation = await _allocation(session_factory, alloc)
        assert (allocation.status, allocation.defined_switch_ids) == ("ACTIVE", [h.switch])
        assert h.calls == []
        assert len(_lost_race_records(caplog)) == 1
        await _assert_invariants(session_factory, h, before)
    finally:
        for engine in (join_engine, check_engine):
            if engine is not None:
                await engine.dispose()
        await _cleanup(session_factory, h)


async def test_a_retried_nil_join_whose_port_a_save_removes_loses_to_the_check(session_factory):
    """A manual retry of a FAILED join with the nil allocation resolves allocation A and
    drives add_to_vlan. Meanwhile a save removes the port: the reconcile settles the
    stale nil row RELEASED with no driver call, and its reservation-wide check, seeing
    no failed join left, releases A and deletes its VLAN. The retry's join then loses at
    its share lock, undoes its add, and its failure write by row id is refused (the row
    is RELEASED): no ACTIVE membership, no row on A, and the port out of the number."""
    from app.services.nats_consumer import _reconcile_l2_memberships
    from app.services.wiring_retry_service import reattempt_reservation

    h = _Harness()
    try:
        await _seed(session_factory, h, own=False)
        async with session_factory() as s:
            nil_row = _membership(h.reservation, NIL, h.switch, "0/0/1", status="FAILED")
            s.add(nil_row)
            await s.commit()
            row_id = nil_row.id
        before = await _foreign_snapshot(session_factory, h)
        retry_engine, _ = _tagged_engine("retry")
        save_engine, _ = _tagged_engine("save")
        h.wires = [h.wire("0/0/1")]
        h.block = "add_to_vlan"
        try:
            with h.patches():
                retry = asyncio.create_task(
                    reattempt_reservation(
                        h.reservation,
                        _session_ctx(async_sessionmaker(retry_engine, expire_on_commit=False)),
                    )
                )
                assert await asyncio.to_thread(h.blocked.wait, COORDINATION_TIMEOUT)
                (alloc,) = (await _own_allocations(session_factory, h)).values()
                h.wires = []
                await _reconcile_l2_memberships(
                    str(h.reservation),
                    [],
                    _FetchContext(None),
                    _session_ctx(async_sessionmaker(save_engine, expire_on_commit=False)),
                )
                released = (await _allocation(session_factory, alloc.id)).status
                h.unblock.set()
                result = await asyncio.wait_for(retry, COORDINATION_TIMEOUT)
        finally:
            h.unblock.set()
            await retry_engine.dispose()
            await save_engine.dispose()
        await _hide_failed(session_factory, h)

        assert released == "RELEASED", "the save's check released A while the join was in flight"
        assert h.actions() == ["create_vlan", "add_to_vlan", "delete_vlan", "remove_from_vlan"]
        assert [r["outcome"] for r in result["results"]] == ["released"]
        rows = await _own_memberships(session_factory, h)
        assert [(r.id, r.status) for r in rows] == [(row_id, "RELEASED")]
        await _assert_invariants(session_factory, h, before)
    finally:
        await _cleanup(session_factory, h)


async def test_a_teardown_against_a_reconcile_whose_first_join_is_in_flight(session_factory):
    """A reconcile passed its frozen gate before the reservation froze and is driving
    its first join on a fresh allocation A (no row yet). The terminal teardown finds no
    L2 row and its reservation-wide check, on the frozen reservation, releases A and
    deletes its VLAN. The join then lands: the record-time freeze check parks it FAILED
    intended RELEASED on A (no allocation lock, WIRE-LEDGER-4), so the port's
    membership in A's number stays tracked, and the next retry removes it. At no point
    is a membership ACTIVE on a RELEASED allocation."""
    from app.services.l2_membership_service import FROZEN_JOIN_PENDING_REMOVAL
    from app.services.nats_consumer import _reconcile_l2_memberships, _teardown_from_ledgers
    from app.services.wiring_retry_service import reattempt_reservation

    h = _Harness()
    try:
        await _seed(session_factory, h, own=False)
        before = await _foreign_snapshot(session_factory, h)
        reconcile_engine, _ = _tagged_engine("reconcile")
        teardown_engine, _ = _tagged_engine("teardown")
        h.wires = [h.wire("0/0/1")]
        h.block = "add_to_vlan"
        try:
            with h.patches():
                reconcile = asyncio.create_task(
                    _reconcile_l2_memberships(
                        str(h.reservation),
                        list(h.wires),
                        _FetchContext(None),
                        _session_ctx(async_sessionmaker(reconcile_engine, expire_on_commit=False)),
                    )
                )
                assert await asyncio.to_thread(h.blocked.wait, COORDINATION_TIMEOUT)
                (alloc,) = (await _own_allocations(session_factory, h)).values()
                await _freeze(session_factory, h.reservation)
                await _teardown_from_ledgers(
                    str(h.reservation),
                    _FetchContext(None),
                    _session_ctx(async_sessionmaker(teardown_engine, expire_on_commit=False)),
                )
                released = (await _allocation(session_factory, alloc.id)).status
                h.unblock.set()
                await asyncio.wait_for(reconcile, COORDINATION_TIMEOUT)
        finally:
            h.unblock.set()
            await reconcile_engine.dispose()
            await teardown_engine.dispose()
        await _hide_failed(session_factory, h)

        assert released == "RELEASED", (
            "the teardown's check released A while the join was in flight"
        )
        assert h.actions() == ["create_vlan", "add_to_vlan", "delete_vlan"]
        (row,) = await _own_memberships(session_factory, h)
        assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
            "FAILED",
            "RELEASED",
            alloc.id,
            FROZEN_JOIN_PENDING_REMOVAL,
        )
        await _assert_invariants(session_factory, h, before)

        h.block = None
        h.calls.clear()
        with h.patches():
            await reattempt_reservation(h.reservation, _session_ctx(session_factory))
        assert h.actions() == ["remove_from_vlan"]
        assert [c[2] for c in h.calls if c[0] == "remove_from_vlan"] == [alloc.vlan_id]
        (row,) = await _own_memberships(session_factory, h)
        assert row.status == "RELEASED"
        await _assert_invariants(session_factory, h, before)
    finally:
        await _cleanup(session_factory, h)
