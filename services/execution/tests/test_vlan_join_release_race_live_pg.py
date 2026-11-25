"""Postgres-live proof that a join and a definition never commit onto a released VLAN
allocation (issue #1413), the join side of the release compare-and-swap of #1357.

The release (`vlan_service.release_allocation_if_orphaned`) matches its row only while it
is ACTIVE with no ACTIVE membership. Before #1413 nothing on the other side checked the
allocation, so a join still uncommitted when the release ran committed an ACTIVE
membership on a RELEASED allocation, and a switch appended to `defined_switch_ids` after
the release winner re-read the row was never undefined. Now:

  - a join takes the allocation row FOR SHARE while it is ACTIVE before its ACTIVE write
    and holds it to its commit, and the release takes the row FOR UPDATE before its
    guarded UPDATE, so exactly one of them wins, in both orders and whichever caller
    releases (the removal pass, or a release with no removal on that allocation, the
    shape of issue #1497's reservation-wide check). The loser of a join is failed for
    the build retry;
  - a define records its switch under the row lock and only while the allocation is
    ACTIVE; a define that lost to a release undoes its create_vlan; two defines of one
    allocation keep both switches; the release winner's trim removes only what it
    deleted from the list as it stands;
  - a failed join on a released allocation is re-resolved by the retry before any
    add_to_vlan, which never names the released number.

The SQLite suites (test_l2_membership_service.py, test_nats_consumer_vlan_definitions.py,
test_wiring_retry_l2.py) pin each guard's predicate on one connection; here each party
is a real asyncio task on its own Postgres connection, and the overlap is staged by
events, never timing (docs/specs/TESTING.md, issue #1473): the holder is stopped inside
its transaction by a gated commit or a gated driver call, the other party is started
only then and is seen waiting on the row lock in pg_stat_activity (by its own
application_name) before the holder is let go, and the gate records whether it was
still holding when that happened. The join, define-against-release, and retry tests
assert the end-state invariant over their own rows: no ACTIVE membership on an
allocation that is not ACTIVE, and every switch create_vlan defined is either deleted
afterwards or still listed on an ACTIVE row.

Inventory, the template read, the driver load, the sandbox, cabling's fabric lookup, and
the intended-wires read are patched; everything from the ledger down is real.

Used ledger (issue #819 and docs/specs/TESTING.md): this suite runs in the gate's
`_gate-pg-live-tests` phase against the gate's already-migrated, already-used execution
schema. Every test runs on random reservation ids, seeds FOREIGN rows under another
random reservation (an ACTIVE allocation with an ACTIVE membership on the switch under
test, a RELEASED allocation, and a RELEASED membership row pointing at the allocation
under test), snapshots every vlan_assignments and l2_port_assignments row it does not
own before and after and asserts nothing changed, asserts it wrote no execution_runs row
for the foreign reservation, and deletes its own and its seeded rows in a finally. Every
FAILED row a test leaves has at least 1,000,000 attempts, far above the default retry cap
of 10, so the gate stack's own retry tick does not select it (the suite drives its one
retry through `_reattempt_l2_rows` directly). The fabric lookup answers a fresh fabric id
for every switch it was not told about, so no allocation already in the ledger reaches
this test's component.

Connections set search_path to the execution schema and a 5 s lock_timeout, so a party
that waits on a lock nobody releases fails in seconds.

Env contract identical to the other execution live-pg suites:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections import defaultdict
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.models.execution_run import ExecutionRun
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.l2_membership_service import (
    JOIN_LOST_ALLOCATION_RELEASED,
    record_l2_membership_active,
)
from app.services.nats_consumer import (
    _define_pending_for_allocations,
    _FetchContext,
    _release_orphaned_allocations,
)
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
COORDINATION_TIMEOUT = 10
# Far above the default retry cap of 10: hides a FAILED row from the gate stack's tick.
HIDDEN_ATTEMPTS = 1_000_000


def _engine(application_name: str | None = None):
    settings = {"search_path": EXECUTION_SCHEMA, "lock_timeout": LOCK_TIMEOUT}
    if application_name is not None:
        settings["application_name"] = application_name
    return create_async_engine(PG_DSN, connect_args={"server_settings": settings})


@pytest.fixture
async def pg_engine():
    engine = _engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def tagged():
    """Engines whose backends carry their own application_name, so a lock poll counts
    only the party it asks about. Disposed after the test."""
    engines: list = []

    def _make(role: str):
        name = f"herd-test-1413-{role}-{uuid.uuid4().hex[:10]}"
        engine = _engine(name)
        engines.append(engine)
        return engine, name

    yield _make
    for engine in engines:
        await engine.dispose()


def _session_ctx(maker):
    """A get_db_session factory: every session it opens checks out its own connection."""

    class _Ctx:
        async def __aenter__(self):
            self._session = maker()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


class _CommitGate:
    """Stops the FIRST commit of the sessions a gated maker opens, before the commit:
    the holder is then inside its transaction with its row locks held. With
    `flush_first` its pending ORM writes are sent first; without it they are not, so a
    holder that read the row FOR UPDATE holds only that lock (the window between a
    locked read and its write). `returned` turns true only once that commit returned."""

    def __init__(self, flush_first: bool = True):
        self.inside = asyncio.Event()
        self.proceed = asyncio.Event()
        self.flush_first = flush_first
        self.used = False
        self.returned = False


def _gated_maker(engine, gate: _CommitGate):
    class _GatedSession(AsyncSession):
        async def commit(self):
            if gate.used:
                return await super().commit()
            gate.used = True
            if gate.flush_first:
                await self.flush()
            gate.inside.set()
            await asyncio.wait_for(gate.proceed.wait(), COORDINATION_TIMEOUT)
            await super().commit()
            gate.returned = True

    return async_sessionmaker(engine, class_=_GatedSession, expire_on_commit=False)


DRIVER_ID = str(uuid.uuid4())
TEMPLATE_DATA = {"id": "tmpl-live", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}


def _device(device_id: str, ctype: str) -> dict:
    return {
        "id": device_id,
        "name": "live-pg",
        "template_id": "tmpl-live",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": ctype,
        "field_data": {"ip": "10.0.0.1"},
    }


class _Harness:
    """Own and foreign ids for one test, the patched upstreams, and the call record.

    `block` names one (action, switch) driver call that blocks in its worker thread until
    `unblock` is set; `blocked` is set when it starts and `block_returned` once it is
    about to return."""

    def __init__(self, n_switches: int = 2):
        self.reservation = uuid.uuid4()
        self.foreign = uuid.uuid4()
        self.fabric = uuid.uuid4()
        self.switches = sorted(str(uuid.uuid4()) for _ in range(n_switches))
        self.dut = str(uuid.uuid4())
        self.calls: list[tuple[str, str, int | None]] = []
        self._lock = threading.Lock()
        self.block: tuple[str, str] | None = None
        self.blocked = threading.Event()
        self.unblock = threading.Event()
        self.block_returned = False
        self._fabrics: dict[str, uuid.UUID] = defaultdict(uuid.uuid4)
        for sid in self.switches:
            self._fabrics[sid] = self.fabric

    def _execute(self, driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        switch = context.get("HERD_device_id")
        with self._lock:
            self.calls.append((action, switch, mk.get("vlan_id")))
        if self.block == (action, switch):
            self.blocked.set()
            self.unblock.wait(COORDINATION_TIMEOUT)
            self.block_returned = True
        return SUCCESS_RESULT

    async def _fetch_device(self, device_id, client=None):
        did = str(device_id)
        return _device(did, "Layer 2 Switch" if did in self.switches else "Server")

    async def _fetch_fabric(self, device_id):
        return self._fabrics[str(device_id)]

    def patches(self, wires=None):
        stack = ExitStack()
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                new=AsyncMock(side_effect=self._fetch_device),
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
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(return_value=wires or []),
            )
        )
        return stack

    def of(self, action: str) -> list[tuple[str, int | None]]:
        return [(sid, vlan) for a, sid, vlan in self.calls if a == action]


async def _free_vlan_numbers(session_factory, count: int) -> list[int]:
    """Numbers no ACTIVE row in the ledger holds, so a test never meets a same-number
    rival it did not seed."""
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
    free = [n for n in range(4094, 1, -1) if n not in held]
    return free[:count]


async def _seed(session_factory, h: _Harness, vlan_id: int, *, scope, defined, status="ACTIVE"):
    """The allocation under test (no ACTIVE member) plus the foreign rows, committed."""
    async with session_factory() as s:
        mine = VlanAssignment(
            reservation_id=h.reservation,
            fabric_id=h.fabric,
            vlan_id=vlan_id,
            switch_device_ids=list(scope),
            defined_switch_ids=list(defined),
            status=status,
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
                # Another reservation's live member on the switch under test, in its own
                # allocation: neither holds nor is touched by the allocation under test.
                L2PortAssignment(
                    reservation_id=h.foreign,
                    vlan_assignment_id=rival.id,
                    switch_device_id=uuid.UUID(h.switches[0]),
                    port="9/9/1",
                    intended="ACTIVE",
                    status="ACTIVE",
                ),
                L2PortAssignment(
                    reservation_id=h.foreign,
                    vlan_assignment_id=mine.id,
                    switch_device_id=uuid.UUID(h.switches[0]),
                    port="9/9/2",
                    intended="RELEASED",
                    status="RELEASED",
                ),
            ]
        )
        await s.commit()
        return mine.id


async def _seed_failed_join(session_factory, h: _Harness, allocation_id, port: str, reason: str):
    async with session_factory() as s:
        row = L2PortAssignment(
            reservation_id=h.reservation,
            vlan_assignment_id=allocation_id,
            switch_device_id=uuid.UUID(h.switches[0]),
            port=port,
            intended="ACTIVE",
            status="FAILED",
            attempts=HIDDEN_ATTEMPTS,
            last_error=reason,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _foreign_snapshot(session_factory, h: _Harness):
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
                    L2PortAssignment.attempts,
                    L2PortAssignment.last_error,
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


async def _assert_invariants(session_factory, h: _Harness, before):
    """No own ACTIVE membership on an allocation that is not ACTIVE; every create_vlan
    the test drove is followed by a delete_vlan of the same switch and number, or the
    switch is listed on an ACTIVE own allocation holding that number; foreign rows are
    unchanged and no foreign execution run was written."""
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
    async with session_factory() as s:
        foreign_runs = (
            await s.execute(select(ExecutionRun.id).where(ExecutionRun.reservation_id == h.foreign))
        ).all()
    assert foreign_runs == []


async def _cleanup(session_factory, h: _Harness):
    ids = [h.reservation, h.foreign]
    async with session_factory() as s:
        await s.execute(delete(L2PortAssignment).where(L2PortAssignment.reservation_id.in_(ids)))
        await s.execute(delete(VlanAssignment).where(VlanAssignment.reservation_id.in_(ids)))
        await s.execute(delete(ExecutionRun).where(ExecutionRun.reservation_id.in_(ids)))
        await s.commit()


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


def _releaser(caller: str, maker, allocation_id, outcomes: list):
    """The release under test as a coroutine: the removal pass (WIRE-VLAN-4), or the
    compare-and-swap called alone with no removal on the allocation (the shape of the
    reservation-wide check of issue #1497)."""
    real = vlan_service.release_allocation_if_orphaned

    if caller == "removal_pass":

        async def _recorded(db, va_id, **kwargs):
            won = await real(db, va_id, **kwargs)
            outcomes.append(won)
            return won

        async def _run():
            with patch("app.services.vlan_service.release_allocation_if_orphaned", new=_recorded):
                await _release_orphaned_allocations(
                    [allocation_id], _session_ctx(maker), _FetchContext(None)
                )

        return _run()

    async def _direct():
        async with maker() as s:
            outcomes.append(await real(s, allocation_id))

    return _direct()


class _Pending:
    """The gates, harness blocks, tasks, and sessions one test starts. `settle` (run in
    every test's finally, before the cleanup) lets every gate and block go, waits for
    the tasks, and closes the sessions, so a test that fails part way never leaves a
    transaction holding the rows its cleanup deletes."""

    def __init__(self):
        self.gates: list[_CommitGate] = []
        self.harnesses: list[_Harness] = []
        self.tasks: list[asyncio.Task] = []
        self.sessions: list[AsyncSession] = []

    def gate(self, flush_first: bool = True) -> _CommitGate:
        gate = _CommitGate(flush_first)
        self.gates.append(gate)
        return gate

    def task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def session(self, maker) -> AsyncSession:
        session = maker()
        self.sessions.append(session)
        return session

    async def settle(self):
        for gate in self.gates:
            gate.proceed.set()
        for h in self.harnesses:
            h.unblock.set()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=COORDINATION_TIMEOUT)
        for task in self.tasks:
            if task.done() and not task.cancelled():
                task.exception()
        for session in self.sessions:
            await session.close()


@pytest.fixture
def pending():
    return _Pending()


@pytest.mark.parametrize("caller", ["removal_pass", "no_removal"])
async def test_a_join_holding_its_lock_makes_the_release_wait_and_lose(
    session_factory, pg_engine, tagged, pending, caller
):
    """The join first: it holds the allocation FOR SHARE with its ACTIVE membership
    sent and uncommitted; the release arrives, waits on the row lock, and once the join
    commits its UPDATE sees the member and matches nothing."""
    h = _Harness()
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=h.switches, defined=h.switches)
        before = await _foreign_snapshot(session_factory, h)
        gate = pending.gate()
        join_engine, _ = tagged("join")
        release_engine, release_app = tagged("release")
        outcomes: list[bool] = []
        with h.patches():
            js = pending.session(_gated_maker(join_engine, gate))
            join = pending.task(
                record_l2_membership_active(
                    js, h.reservation, alloc, uuid.UUID(h.switches[0]), "0/0/1"
                )
            )
            await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)
            release = pending.task(
                _releaser(
                    caller,
                    async_sessionmaker(release_engine, expire_on_commit=False),
                    alloc,
                    outcomes,
                )
            )
            waited = await _seen_waiting(pg_engine, release_app, release)
            join_was_holding = not gate.returned
            gate.proceed.set()
            row = await asyncio.wait_for(join, COORDINATION_TIMEOUT)
            await asyncio.wait_for(release, COORDINATION_TIMEOUT)

        assert join_was_holding
        assert waited, "the release must wait on the join's lock"
        assert outcomes == [False]
        assert (row.status, row.vlan_assignment_id) == ("ACTIVE", alloc)
        assert (await _own_allocations(session_factory, h))[alloc].status == "ACTIVE"
        assert h.of("delete_vlan") == []
        await _assert_invariants(session_factory, h, before)
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


@pytest.mark.parametrize("caller", ["removal_pass", "no_removal"])
async def test_a_release_holding_its_lock_makes_the_join_wait_and_lose(
    session_factory, pg_engine, tagged, pending, caller
):
    """The release first: it has run its guarded UPDATE and holds the row, uncommitted;
    the join arrives, waits on the row lock, then finds the row RELEASED and fails its
    row for the build retry instead of committing an ACTIVE membership."""
    h = _Harness()
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=h.switches, defined=h.switches)
        prior = await _seed_failed_join(session_factory, h, alloc, "0/0/1", "boom")
        before = await _foreign_snapshot(session_factory, h)
        gate = pending.gate()
        join_engine, join_app = tagged("join")
        release_engine, _ = tagged("release")
        outcomes: list[bool] = []
        with h.patches():
            release = pending.task(
                _releaser(caller, _gated_maker(release_engine, gate), alloc, outcomes)
            )
            await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)
            js = pending.session(async_sessionmaker(join_engine, expire_on_commit=False))
            join = pending.task(
                record_l2_membership_active(
                    js, h.reservation, alloc, uuid.UUID(h.switches[0]), "0/0/1"
                )
            )
            waited = await _seen_waiting(pg_engine, join_app, join)
            release_was_holding = not gate.returned
            gate.proceed.set()
            row = await asyncio.wait_for(join, COORDINATION_TIMEOUT)
            await asyncio.wait_for(release, COORDINATION_TIMEOUT)

        assert release_was_holding
        assert waited, "the join must wait on the release's lock"
        assert outcomes == [True]
        assert row.id == prior
        assert (row.status, row.intended, row.vlan_assignment_id) == ("FAILED", "ACTIVE", alloc)
        assert (row.last_error, row.attempts) == (
            JOIN_LOST_ALLOCATION_RELEASED,
            HIDDEN_ATTEMPTS + 1,
        )
        assert (await _own_allocations(session_factory, h))[alloc].status == "RELEASED"
        if caller == "removal_pass":
            assert sorted(h.of("delete_vlan")) == sorted((s, vlan_id) for s in h.switches)
        await _assert_invariants(session_factory, h, before)
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_a_define_whose_create_ran_across_a_release_is_undone(session_factory, pending):
    """The release pass runs to completion while a define's create_vlan is in flight on
    a switch not yet listed: the winner deletes what it read, the define's guarded append
    is refused, and the define deletes its own create, so no definition is orphaned."""
    h = _Harness()
    pending.harnesses.append(h)
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    defined, not_yet = h.switches[0], h.switches[1]
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=h.switches, defined=[defined])
        before = await _foreign_snapshot(session_factory, h)
        h.block = ("create_vlan", not_yet)
        get_db = _session_ctx(session_factory)
        with h.patches():
            define = pending.task(
                _define_pending_for_allocations({alloc}, _FetchContext(None), get_db)
            )
            assert await asyncio.to_thread(h.blocked.wait, COORDINATION_TIMEOUT)
            await _release_orphaned_allocations([alloc], get_db, _FetchContext(None))
            define_was_creating = not h.block_returned
            h.unblock.set()
            failures = await asyncio.wait_for(define, COORDINATION_TIMEOUT)

        assert define_was_creating
        assert failures == {not_yet: (0, JOIN_LOST_ALLOCATION_RELEASED)}
        assert h.of("delete_vlan") == [(defined, vlan_id), (not_yet, vlan_id)]
        row = (await _own_allocations(session_factory, h))[alloc]
        assert (row.status, row.defined_switch_ids) == ("RELEASED", [])
        await _assert_invariants(session_factory, h, before)
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_a_define_holding_its_append_makes_the_release_wait_and_delete_it(
    session_factory, pg_engine, tagged, pending
):
    """The define's append has read the row FOR UPDATE and not yet written when the
    release arrives: the release waits, then releases the row as the define left it,
    so its deletes include the switch just defined."""
    h = _Harness()
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    defined, not_yet = h.switches[0], h.switches[1]
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=h.switches, defined=[defined])
        before = await _foreign_snapshot(session_factory, h)
        gate = pending.gate(flush_first=False)
        define_engine, _ = tagged("define")
        gated = _gated_maker(define_engine, gate)
        release_engine, release_app = tagged("release")
        outcomes: list[bool] = []
        real_record = vlan_service.record_allocation_defined

        async def _gated_record(_db, allocation_id, switch_id):
            # Only the append runs on the gated session: the define's other commits (its
            # execution run rows) are not held.
            return await real_record(pending.session(gated), allocation_id, switch_id)

        with (
            h.patches(),
            patch("app.services.vlan_service.record_allocation_defined", new=_gated_record),
        ):
            define = pending.task(
                _define_pending_for_allocations(
                    {alloc}, _FetchContext(None), _session_ctx(session_factory)
                )
            )
            await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)
            release = pending.task(
                _releaser(
                    "removal_pass",
                    async_sessionmaker(release_engine, expire_on_commit=False),
                    alloc,
                    outcomes,
                )
            )
            waited = await _seen_waiting(pg_engine, release_app, release)
            define_was_holding = not gate.returned
            gate.proceed.set()
            failures = await asyncio.wait_for(define, COORDINATION_TIMEOUT)
            await asyncio.wait_for(release, COORDINATION_TIMEOUT)

        assert define_was_holding
        assert waited, "the release must wait on the define's lock"
        assert failures == {}
        assert outcomes == [True]
        assert sorted(h.of("delete_vlan")) == sorted([(defined, vlan_id), (not_yet, vlan_id)])
        row = (await _own_allocations(session_factory, h))[alloc]
        assert (row.status, row.defined_switch_ids) == ("RELEASED", [])
        await _assert_invariants(session_factory, h, before)
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_a_define_and_a_trim_during_the_winners_deletes(session_factory, pending):
    """While the release winner is inside its delete loop: a guarded define of another
    switch is refused (the row is RELEASED), and a switch listed by a writer outside the
    guard (a replica on the code before #1413 during a rolling deploy) survives the
    winner's trim, which removes only the switch it deleted."""
    h = _Harness(n_switches=3)
    pending.harnesses.append(h)
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    deleted, refused, outside = h.switches
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=[deleted], defined=[deleted])
        before = await _foreign_snapshot(session_factory, h)
        h.block = ("delete_vlan", deleted)
        with h.patches():
            release = pending.task(
                _release_orphaned_allocations(
                    [alloc], _session_ctx(session_factory), _FetchContext(None)
                )
            )
            assert await asyncio.to_thread(h.blocked.wait, COORDINATION_TIMEOUT)
            async with session_factory() as s:
                recorded = await vlan_service.record_allocation_defined(s, alloc, refused)
            async with session_factory() as s:
                await s.execute(
                    update(VlanAssignment)
                    .where(VlanAssignment.id == alloc)
                    .values(defined_switch_ids=[deleted, outside])
                )
                await s.commit()
            winner_was_deleting = not h.block_returned
            h.unblock.set()
            await asyncio.wait_for(release, COORDINATION_TIMEOUT)

        assert winner_was_deleting
        assert recorded is False
        row = (await _own_allocations(session_factory, h))[alloc]
        assert (row.status, row.defined_switch_ids) == ("RELEASED", [outside])
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_the_trim_reads_and_writes_in_one_critical_section(
    session_factory, pg_engine, tagged, pending
):
    """The trim has read the released row FOR UPDATE and not yet written when another
    writer appends a switch in one statement: the writer waits, and appends to the list
    the trim wrote, so neither change is lost."""
    h = _Harness(n_switches=2)
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    deleted, outside = h.switches
    try:
        alloc = await _seed(
            session_factory, h, vlan_id, scope=[deleted], defined=[deleted], status="RELEASED"
        )
        before = await _foreign_snapshot(session_factory, h)
        gate = pending.gate(flush_first=False)
        trim_engine, _ = tagged("trim")
        writer_engine, writer_app = tagged("writer")
        ts = pending.session(_gated_maker(trim_engine, gate))
        trim = pending.task(vlan_service.trim_allocation_definitions(ts, alloc, [deleted]))
        await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)

        async def _append():
            async with writer_engine.connect() as conn:
                await conn.execute(
                    text(
                        "UPDATE vlan_assignments SET defined_switch_ids = "
                        "(defined_switch_ids::jsonb || jsonb_build_array(CAST(:sid AS text)))"
                        "::json WHERE id = :id"
                    ),
                    {"sid": outside, "id": alloc},
                )
                await conn.commit()

        writer = pending.task(_append())
        waited = await _seen_waiting(pg_engine, writer_app, writer)
        trim_was_holding = not gate.returned
        gate.proceed.set()
        await asyncio.wait_for(asyncio.gather(trim, writer), COORDINATION_TIMEOUT)

        assert trim_was_holding
        assert waited, "the writer must wait on the trim's lock"
        row = (await _own_allocations(session_factory, h))[alloc]
        assert row.defined_switch_ids == [outside]
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_two_defines_of_one_allocation_keep_both_switches(
    session_factory, pg_engine, tagged, pending
):
    """One define has read the row FOR UPDATE and not yet written; the other waits on
    the row lock and then appends to the list as the first left it."""
    h = _Harness()
    (vlan_id,) = await _free_vlan_numbers(session_factory, 1)
    first, second = h.switches
    try:
        alloc = await _seed(session_factory, h, vlan_id, scope=h.switches, defined=[])
        before = await _foreign_snapshot(session_factory, h)
        gate = pending.gate(flush_first=False)
        first_engine, _ = tagged("define-a")
        second_engine, second_app = tagged("define-b")
        a = pending.session(_gated_maker(first_engine, gate))
        holder = pending.task(vlan_service.record_allocation_defined(a, alloc, first))
        await asyncio.wait_for(gate.inside.wait(), COORDINATION_TIMEOUT)
        b = pending.session(async_sessionmaker(second_engine, expire_on_commit=False))
        other = pending.task(vlan_service.record_allocation_defined(b, alloc, second))
        waited = await _seen_waiting(pg_engine, second_app, other)
        holder_was_holding = not gate.returned
        gate.proceed.set()
        results = await asyncio.wait_for(asyncio.gather(holder, other), COORDINATION_TIMEOUT)

        assert holder_was_holding
        assert waited, "the second define must wait on the first one's lock"
        assert results == [True, True]
        row = (await _own_allocations(session_factory, h))[alloc]
        assert row.defined_switch_ids == sorted([first, second])
        assert await _foreign_snapshot(session_factory, h) == before
    finally:
        await pending.settle()
        await _cleanup(session_factory, h)


async def test_the_retry_of_a_failed_join_on_a_released_allocation_drives_a_fresh_one(
    session_factory,
):
    """The row a lost join leaves, on an allocation released since: the retry resolves
    a fresh ACTIVE allocation before its driver call, add_to_vlan names that number and
    never the released one, and the row ends ACTIVE on the fresh allocation."""
    from app.services.vlan_service import _derive_vlan_id
    from app.services.wiring_retry_service import _reattempt_l2_rows

    h = _Harness(n_switches=1)
    # Not the reservation's preferred number, which the fresh allocation takes.
    preferred = _derive_vlan_id(str(h.reservation))
    released_vlan = next(n for n in await _free_vlan_numbers(session_factory, 2) if n != preferred)
    switch = h.switches[0]
    wires = [
        {
            "device_a_id": h.dut,
            "port_a": "eth0",
            "device_b_id": switch,
            "port_b": "0/0/1",
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": None,
        }
    ]
    try:
        released = await _seed(
            session_factory, h, released_vlan, scope=[switch], defined=[], status="RELEASED"
        )
        rid = await _seed_failed_join(
            session_factory, h, released, "0/0/1", JOIN_LOST_ALLOCATION_RELEASED
        )
        before = await _foreign_snapshot(session_factory, h)
        async with session_factory() as s:
            rows = list(
                (await s.execute(select(L2PortAssignment).where(L2PortAssignment.id == rid)))
                .scalars()
                .all()
            )
        with h.patches(wires=wires):
            outcomes = await _reattempt_l2_rows(rows, _session_ctx(session_factory))

        allocations = await _own_allocations(session_factory, h)
        (fresh,) = [a for a in allocations.values() if a.status == "ACTIVE"]
        assert fresh.id != released and fresh.vlan_id != released_vlan
        assert h.of("add_to_vlan") == [(switch, fresh.vlan_id)]
        (row,) = await _own_memberships(session_factory, h)
        assert (row.id, row.status, row.vlan_assignment_id) == (rid, "ACTIVE", fresh.id)
        assert [o["outcome"] for o in outcomes] == ["reconnected"]
        await _assert_invariants(session_factory, h, before)
    finally:
        await _cleanup(session_factory, h)
