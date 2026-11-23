"""Postgres-live proof of the reservation-wide allocation check against a concurrent
join (issue #1497, ADR 0018 D11, WIRE-VLAN-4).

`_release_reservation_orphaned_allocations` reads its candidates (the reservation's
ACTIVE allocations with no ACTIVE member that `held_by_a_pending_join` does not hold)
and releases each through `release_allocation_if_orphaned(..., reservation_wide=True)`,
whose one guarded UPDATE re-checks both the membership and the pending-join condition.
The SQLite suite (tests/test_reservation_wide_allocation_check.py) pins the predicate on
one connection. Here the check runs on its own Postgres connections while another
connection commits a join of the same reservation:

  - a FAILED intended-ACTIVE join (nil allocation, the WIRE-RETRY-8 shape) committed
    after the candidate read and before the UPDATE keeps every allocation of the
    reservation ACTIVE, drives no delete_vlan, and logs the lost race;
  - a join that flips to an ACTIVE membership of the allocation in that window keeps
    it ACTIVE the same way;
  - the frozen fact and the failed joins are read for the allocation's OWN reservation:
    another reservation that is frozen and has a FAILED join changes nothing, and a
    frozen reservation's own orphan is released;
  - the known limit (#1413's class): a failed join committed while the UPDATE waits on
    the allocation's row lock is not seen, because the guard is evaluated against the
    UPDATE's own snapshot, so the allocation is released. Pinned so the spec sentence
    stays true.

Inventory, the template read, the driver load, the sandbox, and cabling's fabric lookup
are patched; everything from the ledger down is real.

Used ledger (issue #819 and docs/specs/TESTING.md): every test runs on random
reservation ids, seeds FOREIGN rows under other random reservations (one frozen with a
FAILED join and an orphaned ACTIVE allocation, one not frozen with an ACTIVE
allocation holding a member), snapshots every vlan_assignments, l2_port_assignments,
and reservation_wiring_state row it does not own before and after and asserts nothing
changed, and deletes its own and its seeded rows in a finally. The fabric lookup
answers a fresh fabric id for every switch, so no allocation already in the ledger can
read as superseding.

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
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.nats_consumer import _FetchContext, _release_reservation_orphaned_allocations
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
    def __init__(self):
        self.reservation = uuid.uuid4()
        self.foreign_frozen = uuid.uuid4()
        self.foreign_live = uuid.uuid4()
        self.switch = str(uuid.uuid4())
        self.calls: list[tuple[str, str, int]] = []
        self._fabrics: dict[str, uuid.UUID] = defaultdict(uuid.uuid4)

    @property
    def ids(self):
        return [self.reservation, self.foreign_frozen, self.foreign_live]

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
        attempts=1 if status == "FAILED" else 0,
        last_error="driver timeout" if status == "FAILED" else None,
    )


async def _seed(session_factory, h: _Harness, *, frozen: bool = False) -> uuid.UUID:
    """The allocation under test (ACTIVE, defined on one switch, no member) of
    h.reservation, plus the foreign rows; returns the allocation id."""
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
        s.add_all([mine, foreign_orphan, foreign_held])
        await s.flush()
        s.add_all(
            [
                _membership(
                    h.foreign_frozen, foreign_orphan.id, foreign_switch, "0/0/1", status="FAILED"
                ),
                _membership(h.foreign_live, foreign_held.id, foreign_switch, "0/0/2"),
                # History of another reservation pointing at the allocation under test.
                _membership(
                    h.foreign_live,
                    mine.id,
                    h.switch,
                    "0/0/3",
                    status="RELEASED",
                    intended="RELEASED",
                ),
            ]
        )
        await s.commit()
        mine_id = mine.id
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
                    "AND application_name = :app AND query ILIKE 'UPDATE vlan_assignments%'"
                ),
                {"app": application_name},
            )
        ).scalar_one()


async def test_known_limit_a_failed_join_committed_while_the_update_waits_is_not_seen(
    pg_engine, session_factory
):
    """The guard is evaluated in the UPDATE's own snapshot. A failed join that commits
    after that snapshot (here, while the UPDATE waits on the allocation's row lock held
    by a third connection) is not seen and the allocation is released: the same class
    as #1413's uncommitted join, pinned so WIRE-VLAN-4 states it truthfully."""
    h = _Harness()
    checker_engine = None
    try:
        alloc = await _seed(session_factory, h)
        before = await _foreign_snapshot(session_factory, h)
        app_name = f"herd-test-1497-{uuid.uuid4().hex[:12]}"
        checker_engine = _engine(app_name)
        get_db = _session_ctx(async_sessionmaker(checker_engine, expire_on_commit=False))
        with h.patches():
            async with pg_engine.connect() as holder:
                await holder.execute(
                    select(VlanAssignment.id).where(VlanAssignment.id == alloc).with_for_update()
                )
                task = asyncio.create_task(_check(h, get_db))
                for _ in range(200):
                    if await _waiting_on_lock(pg_engine, app_name) == 1:
                        break
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail("the guarded UPDATE never queued on the allocation's row lock")
                async with session_factory() as s:
                    s.add(_membership(h.reservation, NIL, h.switch, "0/0/9", status="FAILED"))
                    await s.commit()
                await holder.rollback()
            await asyncio.wait_for(task, timeout=30)
        assert (await _allocation(session_factory, alloc)).status == "RELEASED"
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        if checker_engine is not None:
            await checker_engine.dispose()
        await _cleanup(session_factory, h)
