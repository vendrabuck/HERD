"""Postgres-live proof that the per-row drive claim serializes the two retry channels.

Issue #817. The manual channel (`reattempt_reservation`) and the background channel
(`run_wiring_retry_tick`) load hardware-retryable FAILED ledger rows and drive them
through the same consumer applies. Nothing claimed a row before driving it, so both
could call the driver for one row, from two processes even (the tick starts in every
execution replica). The fix stamps `claimed_until` under a compare-and-swap
immediately before each row's own driver call.

The SQLite unit suite (tests/test_wiring_claim.py) pins the compare-and-swap, the
selection predicate, the clearing, and the `in_progress` reporting, but it cannot
prove the two channels genuinely serialize: its "concurrency" is sequential calls in
one event loop with one connection. This file is the missing other half. Each test:

  - seeds ONE FAILED row for one throwaway reservation, in the real `execution`
    schema, through the real ORM models;
  - runs the claim holder as a REAL asyncio task and the other channel in the same
    loop, over SEPARATE Postgres sessions;
  - serves them a GATED driver stub (issue #1473): the holder's first row action
    signals that it is inside its drive and then blocks, in the worker thread the
    production code runs it in (`asyncio.to_thread`), until the test releases it.
    The test starts the losing channel only after that signal and releases the
    holder only after the loser has returned, so the loser runs strictly inside the
    holder's claim on any runner speed; no fixed sleep decides an outcome, and every
    wait is bounded and fails loudly;
  - asserts the driver was called EXACTLY ONCE for the row, and that the channel
    that lost the claim reported the row rather than dropping it: `in_progress` for
    the manual channel; for the tick, either the row is not a candidate at all
    (`rows_due` 0) or, when it selected the row before the claim, `in_progress`.

The rows are RELEASE-direction (intended RELEASED) on purpose. A release needs no
cabling fork fetch and no intent revalidation, so the test exercises the claim and
the driver path without standing up the whole intent-derivation chain; the claim hook
is the same code on both directions. Inventory, the template fetch, the driver load
and the sandbox are patched; everything from the ledger down is the real thing.

Env contract identical to services/cabling/tests/test_fork_restore_save_race_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Every test uses a fresh random reservation_id (no cross-schema FKs, so nothing else
references it) and deletes its own rows in a finally.

Gate-ledger scoping (issue #819). The Makefile comment above `_gate-pg-live-tests`
records that this suite, like the cabling live-pg suites before it, runs against the
gate's ALREADY-MIGRATED, ALREADY-USED execution schema and must leave its data alone;
it is not a throwaway database. The nightly of 2026-09-15 found this suite broke that
contract: the gate's execution ledger already held FAILED rows from the seeded e2e
phase that runs earlier in the same gate, `run_wiring_retry_tick`'s real
`due_failed_rows`/`due_failed_l2_rows`/`due_failed_route_rows` selects picked those
foreign rows up alongside the test's own row, `stats["rows_due"]` counted them, and
(when a foreign row's switch happened to resolve through the patched `_fetch_device`)
the tick drove and flipped them through the slow stub, mutating rows this suite does
not own. Every test now patches the three `due_failed_*` names as imported by
`wiring_retry_service` (see `_patches`) with wrappers that call the REAL query, so the
claim predicate and its `FOR UPDATE SKIP LOCKED` are still exercised end to end, and
then keep only the rows whose `reservation_id` is this test's own. The background
channel's view of the ledger is therefore scoped to one reservation without touching
the selection SQL itself. `test_a_row_selected_before_it_was_claimed_loses_the_drive_time_cas`
calls `due_failed_rows` directly (bypassing the tick, and so the scoping patch, by
design) and now asserts its own row is somewhere IN that real, unscoped result rather
than the only row in it, and drives only its own preselected row forward. Every test
also snapshots every OTHER reservation's ledger rows before and after the race and
asserts nothing about them changed, so a regression here fails loudly instead of
quietly corrupting a shared gate stack again.

Isolation from a stack's own tick (issue #1473). Scoping narrows what THIS process's
tick sees; it does nothing about the other direction. The gate stack's execution
service runs its own retry tick against the same ledger, and on the 2026-11-19 nightly
it selected a row this suite had just seeded, could not find the suite's random switch
in the real inventory, and recorded the pinned non-retryable reason over the row while
this suite's tick was driving it (the claim-blind failure write fixed by issue #1479).
Every row this suite seeds now carries HIDDEN_ATTEMPTS, above any real retry cap, so a
stack's due selects never return it, and this process's tick runs with TEST_TICK_CAP
above that (the issue #1190 suite's technique).
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.models.execution_run import ExecutionRun
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.l1_assignment_service import due_failed_rows as _real_due_failed_rows
from app.services.l2_membership_service import due_failed_l2_rows as _real_due_failed_l2_rows
from app.services.route_service import due_failed_route_rows as _real_due_failed_route_rows
from app.services.wiring_retry_service import reattempt_reservation, run_wiring_retry_tick
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


def _run_sync_reachable() -> bool:
    return asyncio.run(_pg_reachable())


_PG_REACHABLE = _run_sync_reachable()

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


_SCHEMA_TABLES = (
    L1ConnectionAssignment,
    L2PortAssignment,
    RouteAssignment,
    VlanAssignment,
    ReservationWiringState,
    ExecutionRun,
)


@pytest.fixture(autouse=True)
def _use_execution_schema():
    """Point the ORM Table objects at the real "execution" schema for this file only.

    services/execution/conftest.py forces DB_SCHEMA="" for the SQLite unit suite
    (Settings is a process-wide singleton fixed at first import), so these mapped
    Table objects carry schema=None by the time this module runs, while the live
    database's migrations created them under "execution". Restored in the finally so
    a combined `pytest tests/` run leaves every other file's SQLite queries alone.
    """
    originals = tuple(m.__table__.schema for m in _SCHEMA_TABLES)
    for m in _SCHEMA_TABLES:
        m.__table__.schema = "execution"
    try:
        yield
    finally:
        for m, schema in zip(_SCHEMA_TABLES, originals, strict=True):
            m.__table__.schema = schema


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(PG_DSN)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database, the
    # production session factory shape every service actually runs with.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


def _session_ctx(session_factory):
    """A get_db_session factory of the shape both retry channels expect."""

    class _Ctx:
        async def __aenter__(self):
            self._session = session_factory()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = uuid.uuid4()
DRIVER_ID = uuid.uuid4()
SWITCH_DATA = {
    "id": str(SWITCH_ID),
    "name": "live-pg-switch",
    "template_id": "tmpl-1",
    "driver_id": str(DRIVER_ID),
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "Template", "sections": []}
SUCCESS = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}

# Issue #1473, the issue #1190 suite's isolation: every row this suite seeds carries
# this many attempts. A stack's own retry tick selects only rows with
# `attempts < wiring_retry_max_attempts` (10 by default), in the SQL of its three due
# selects, so it never loads, claims, drives, or writes these rows. This process's own
# tick runs with TEST_TICK_CAP (see _patches), which is above it. The manual channel
# ignores the cap by design, so it needs nothing.
HIDDEN_ATTEMPTS = 1_000_000
TEST_TICK_CAP = HIDDEN_ATTEMPTS + 1
# The raised cap makes every FAILED row of a used ledger a candidate of the real due
# selects, oldest first, so the scoped selects (and the direct preselects) use a limit
# no gate ledger reaches instead of the production batch size, or older foreign rows
# could push this test's own newest row out of the result.
SCOPED_SELECT_LIMIT = 10000

# Driver actions that mean "this row was actually driven". login/logout are
# per-switch bookkeeping and say nothing about the row.
_ROW_ACTIONS = (
    "connect_ports",
    "disconnect_ports",
    "add_to_vlan",
    "remove_from_vlan",
    "configure_route",
    "remove_route",
)

# Issue #1473: the races are staged with explicit coordination, never a fixed sleep.
# A generous bound for every wait on the coordination events: a wait that expires
# fails the test loudly, it never lets the test proceed on a guess. A healthy run
# never waits anywhere near it.
COORDINATION_TIMEOUT = 30.0
_LOGIN_REFUSED = {"success": False, "output": None, "error": "login refused", "duration_ms": 1}


def _gated_driver_stub():
    """A sandbox stub whose FIRST row action blocks until the test releases it.

    `entered` is set when that first row action begins: the claim holder is inside its
    drive, and its claim is committed (every layer claims before its row's first driver
    call). The stub then waits on `release`, which the test sets only after the losing
    channel has returned, so the holder cannot finish its drive before the loser has
    run, on any runner speed. Every later row action returns at once.
    `state["fail_logins"]`, when true, makes a login answer a driver failure. The stub
    runs in the worker thread `nats_consumer._run_sandbox` hands it, so its waits never
    block the event loop; `release.wait` is bounded and records an expiry in
    `state["release_timed_out"]` for the test to fail on.
    """
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    state = {"fail_logins": False, "blocked": False, "release_timed_out": False}

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action == "login" and state["fail_logins"]:
            return _LOGIN_REFUSED
        if action in _ROW_ACTIONS and not state["blocked"]:
            state["blocked"] = True
            entered.set()
            if not release.wait(COORDINATION_TIMEOUT):
                state["release_timed_out"] = True
        return SUCCESS

    return execute_fn, calls, entered, release, state


async def _wait_entered(entered: threading.Event, holder: asyncio.Task | None = None) -> None:
    """Wait, off the loop, until the holder is inside its drive; fail loudly otherwise.

    With `holder` given, a holder that finishes without ever entering its drive fails
    at once instead of after the full timeout.
    """
    waiter = asyncio.ensure_future(asyncio.to_thread(entered.wait, COORDINATION_TIMEOUT))
    if holder is not None:
        await asyncio.wait({waiter, holder}, return_when=asyncio.FIRST_COMPLETED)
        if not waiter.done():
            entered.set()  # free the waiting thread
            await waiter
            holder.result()  # surface the holder's own exception, if it raised
            pytest.fail("the claim holder finished without entering its drive")
    if not await waiter:
        pytest.fail(f"the claim holder never entered its drive in {COORDINATION_TIMEOUT}s")


async def _loser_inside_holders_drive(holder_coro, run_loser, stub_state, entered, release):
    """Run `run_loser()` strictly inside the drive of the task `holder_coro` starts.

    The order is fixed by events, not timing: start the holder; wait until the stub
    reports it inside its drive (its claim committed); run the loser to completion;
    only then release the stub. Returns (holder_result, loser_result) and fails if the
    holder had finished before the loser returned or the stub's release wait expired.
    """
    holder = asyncio.create_task(holder_coro)
    holder_was_driving = False
    try:
        await _wait_entered(entered, holder)
        loser_result = await run_loser()
        holder_was_driving = not holder.done()
    finally:
        release.set()
        holder_result = await asyncio.wait_for(holder, COORDINATION_TIMEOUT)
    assert holder_was_driving, "the loser must have run while the holder was still driving"
    assert not stub_state["release_timed_out"], "the stub's release wait expired"
    return holder_result, loser_result


def _scope_to_reservation(real_fn, reservation_id: uuid.UUID):
    """Wrap a real `due_failed_*` query so it answers for ONE reservation only.

    The real function still runs with the real arguments: the FAILED/attempts-cap
    predicate and the `FOR UPDATE SKIP LOCKED` claim exclusion are exercised exactly
    as the tick would exercise them. Only the returned rows are narrowed, after the
    fact, to this test's own reservation, so a gate ledger holding other reservations'
    FAILED rows (issue #819) never becomes a candidate for this test's tick.
    """

    async def _scoped(db, limit, max_attempts, now=None):
        rows = await real_fn(db, max(limit, SCOPED_SELECT_LIMIT), max_attempts, now)
        return [r for r in rows if r.reservation_id == reservation_id]

    return _scoped


def _patches(execute_fn, reservation_id: uuid.UUID):
    async def _device(device_id, client=None):
        if str(device_id) == str(SWITCH_ID):
            return SWITCH_DATA
        return {
            "id": str(device_id),
            "name": "dut",
            "connection_type": "Server",
            "field_data": {},
        }

    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template",
            new=AsyncMock(return_value=TEMPLATE_DATA),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        # issue #1473: this process's tick must still select the hidden rows.
        patch.object(settings, "wiring_retry_max_attempts", TEST_TICK_CAP),
        # issue #819: scope the background channel's ledger view to this test's own
        # reservation, on every test, so a used gate ledger's foreign FAILED rows are
        # never selected, driven, or counted by this test's tick.
        patch(
            "app.services.wiring_retry_service.due_failed_rows",
            new=_scope_to_reservation(_real_due_failed_rows, reservation_id),
        ),
        patch(
            "app.services.wiring_retry_service.due_failed_l2_rows",
            new=_scope_to_reservation(_real_due_failed_l2_rows, reservation_id),
        ),
        patch(
            "app.services.wiring_retry_service.due_failed_route_rows",
            new=_scope_to_reservation(_real_due_failed_route_rows, reservation_id),
        ),
    ]


async def _foreign_ledger_snapshot(session_factory, reservation_id: uuid.UUID) -> dict:
    """Every ledger row NOT owned by this test, keyed by (kind, id): (status, claimed_until).

    On a throwaway empty Postgres this is an empty dict and the comparison callers
    make against it is trivially true. On the gate's used ledger it is nonempty, and
    it is the load-bearing check (issue #819) that a scoped tick left every other
    reservation's rows completely alone: unchanged status, unchanged claimed_until.
    """
    rows: dict[tuple[str, uuid.UUID], tuple[str, object]] = {}
    async with session_factory() as db:
        for kind, model in (
            ("l1", L1ConnectionAssignment),
            ("l2", L2PortAssignment),
            ("l3", RouteAssignment),
        ):
            result = await db.execute(select(model).where(model.reservation_id != reservation_id))
            for row in result.scalars().all():
                rows[(kind, row.id)] = (row.status, row.claimed_until)
    return rows


async def _cleanup(session_factory, reservation_id: uuid.UUID) -> None:
    async with session_factory() as db:
        for model in (L1ConnectionAssignment, L2PortAssignment, RouteAssignment, ExecutionRun):
            await db.execute(delete(model).where(model.reservation_id == reservation_id))
        await db.execute(
            delete(VlanAssignment).where(VlanAssignment.reservation_id == reservation_id)
        )
        await db.execute(
            delete(ReservationWiringState).where(
                ReservationWiringState.reservation_id == reservation_id
            )
        )
        await db.commit()


async def _race(session_factory, reservation_id, entered, release, state):
    """The manual channel holds the claim; the whole tick runs inside its drive.

    Returns (manual_result, tick_stats). The manual call runs as a task until the stub
    reports it inside its drive (its claim committed), the tick then runs to
    completion, and only then is the manual call's drive released
    (_loser_inside_holders_drive). Each channel gets its OWN session factory instance
    so neither shares a connection or a transaction with the other: the serialization
    under test has to come from the database, not from a shared session.
    """
    return await _loser_inside_holders_drive(
        reattempt_reservation(reservation_id, _session_ctx(session_factory)),
        lambda: run_wiring_retry_tick(_session_ctx(session_factory)),
        state,
        entered,
        release,
    )


def _row_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c in _ROW_ACTIONS]


@pytest.mark.asyncio
async def test_seeded_rows_are_invisible_to_a_stack_tick(session_factory):
    """Issue #1473: the three real due selects, at the cap a stack runs with (this
    process's unpatched setting), return none of the rows this suite seeds, while the
    same selects at TEST_TICK_CAP do."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        alloc = VlanAssignment(reservation_id=reservation_id, fabric_id=uuid.uuid4(), vlan_id=1)
        db.add(alloc)
        await db.flush()
        rows = [
            L1ConnectionAssignment(
                reservation_id=reservation_id,
                switch_device_id=SWITCH_ID,
                port_a="0/0/31",
                port_b="0/0/32",
                status="FAILED",
                intended="RELEASED",
                attempts=HIDDEN_ATTEMPTS,
                last_error="driver timeout",
            ),
            L2PortAssignment(
                reservation_id=reservation_id,
                vlan_assignment_id=alloc.id,
                switch_device_id=SWITCH_ID,
                port="0/0/33",
                status="FAILED",
                intended="RELEASED",
                attempts=HIDDEN_ATTEMPTS,
                last_error="driver timeout",
            ),
            RouteAssignment(
                reservation_id=reservation_id,
                device_id=SWITCH_ID,
                routes=[{"destination": "198.51.100.0/24", "next_hop": "198.51.100.1"}],
                status="FAILED",
                intended="RELEASED",
                attempts=HIDDEN_ATTEMPTS,
                last_error="driver timeout",
            ),
        ]
        db.add_all(rows)
        await db.commit()
        own = {r.id for r in rows}

    stack_cap = settings.wiring_retry_max_attempts
    assert stack_cap < HIDDEN_ATTEMPTS
    selects = (_real_due_failed_rows, _real_due_failed_l2_rows, _real_due_failed_route_rows)
    try:
        async with session_factory() as db:
            seen_by_stack = set()
            seen_by_test = set()
            for due in selects:
                seen_by_stack |= {r.id for r in await due(db, SCOPED_SELECT_LIMIT, stack_cap)}
                seen_by_test |= {r.id for r in await due(db, SCOPED_SELECT_LIMIT, TEST_TICK_CAP)}
            await db.rollback()
    finally:
        await _cleanup(session_factory, reservation_id)

    assert seen_by_stack & own == set(), "a stack's tick must never select this suite's rows"
    assert own <= seen_by_test, "this process's tick, at TEST_TICK_CAP, still selects them"


@pytest.mark.asyncio
async def test_l1_row_is_driven_exactly_once_across_both_channels(session_factory):
    """One FAILED L1 cross-connect, both channels racing: one disconnect_ports call."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/1",
            port_b="0/0/2",
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id, entered, release, state)
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["disconnect_ports"], (
        f"the row must reach the driver exactly once across both channels, got {_row_calls(calls)}"
    )
    outcomes = {r["id"]: r["outcome"] for r in manual["results"]}
    assert outcomes == {str(row_id): "released"}, "the winner reports the real outcome"
    # The manual channel holds the claim for the whole tick (the tick runs only after
    # the stub reports the manual drive entered, and the drive is released only after
    # the tick returns), so the tick's own claim predicate excludes the row before its
    # drive-time compare-and-swap is even reached: nothing due, nothing driven,
    # nothing counted as failed. The drive-time loss is proved by
    # test_a_row_selected_before_it_was_claimed_loses_the_drive_time_cas below. rows_due is
    # scoped to this reservation (issue #819), so a used gate ledger's foreign FAILED
    # rows never make this assertion fail.
    assert stats["rows_due"] == 0, (
        "a claimed row is not a candidate for the other channel, in this reservation's "
        "scoped view of the ledger"
    )
    assert stats["rows_retried"] == 0
    assert stats["still_failed"] == 0
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before, (
        "a scoped tick must never change another reservation's ledger rows"
    )


@pytest.mark.asyncio
async def test_l2_row_is_driven_exactly_once_across_both_channels(session_factory):
    """One FAILED L2 membership, both channels racing: one remove_from_vlan call."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        alloc = VlanAssignment(
            reservation_id=reservation_id,
            fabric_id=uuid.uuid4(),
            vlan_id=1001,
        )
        db.add(alloc)
        await db.flush()
        row = L2PortAssignment(
            reservation_id=reservation_id,
            vlan_assignment_id=alloc.id,
            switch_device_id=SWITCH_ID,
            port="0/0/5",
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id, entered, release, state)
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["remove_from_vlan"], (
        "the membership must reach the driver exactly once across both channels, "
        f"got {_row_calls(calls)}"
    )
    outcomes = {r["id"]: r["outcome"] for r in manual["results"]}
    assert outcomes == {str(row_id): "released"}
    # Scoped to this reservation (issue #819): a used gate ledger's foreign FAILED
    # rows never make this assertion fail.
    assert stats["rows_due"] == 0, "the tick's ledger view is scoped to this reservation"
    assert stats["rows_retried"] == 0
    assert stats["still_failed"] == 0
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before, (
        "a scoped tick must never change another reservation's ledger rows"
    )


@pytest.mark.asyncio
async def test_l3_pin_is_driven_exactly_once_across_both_channels(session_factory):
    """One FAILED L3 route pin, both channels racing: one remove_route call."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = RouteAssignment(
            reservation_id=reservation_id,
            device_id=SWITCH_ID,
            routes=[{"destination": "198.51.100.0/24", "next_hop": "198.51.100.1"}],
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id, entered, release, state)
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["remove_route"], (
        f"the pin must reach the driver exactly once across both channels, got {_row_calls(calls)}"
    )
    outcomes = {r["id"]: r["outcome"] for r in manual["results"]}
    assert outcomes == {str(row_id): "released"}
    # Scoped to this reservation (issue #819): a used gate ledger's foreign FAILED
    # rows never make this assertion fail.
    assert stats["rows_due"] == 0, "the tick's ledger view is scoped to this reservation"
    assert stats["rows_retried"] == 0
    assert stats["still_failed"] == 0
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before, (
        "a scoped tick must never change another reservation's ledger rows"
    )


@pytest.mark.asyncio
async def test_the_manual_channel_reports_in_progress_when_the_tick_wins(session_factory):
    """The mirror ordering: the tick holds the claim, so the MANUAL call is the one
    that loses it and must report `in_progress` rather than omitting the row (an
    absent row would read to the caller as one that was already fixed). The manual
    call runs start to finish while the tick's drive is held open by the stub."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/7",
            port_b="0/0/8",
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        stats, manual_result = await _loser_inside_holders_drive(
            run_wiring_retry_tick(_session_ctx(session_factory)),
            lambda: reattempt_reservation(reservation_id, _session_ctx(session_factory)),
            state,
            entered,
            release,
        )
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["disconnect_ports"]
    outcomes = {r["id"]: r["outcome"] for r in manual_result["results"]}
    assert outcomes == {str(row_id): "in_progress"}, (
        "the losing manual call must REPORT the row, as in_progress"
    )
    assert stats["released"] == 1, "the tick, holding the claim, drove and released it"
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before, (
        "a scoped tick must never change another reservation's ledger rows"
    )


@pytest.mark.asyncio
async def test_a_row_selected_before_it_was_claimed_loses_the_drive_time_cas(session_factory):
    """The drive-time compare-and-swap, isolated and made deterministic.

    In the tests above the loser is stopped by the SELECTION predicate: by the time it
    queries, the winner's stamp is already committed, so the row is not even a
    candidate. That is the common case, but it is not the window issue #817 is really
    about. The dangerous window is a channel that selected the row while it WAS free
    and only reaches its driver call later, after the other channel claimed it: one
    batch is driven sequentially behind a per-switch login, so a row can sit in a
    loaded batch for minutes.

    That window is reproduced exactly here rather than left to task-start luck: the
    row is loaded through the tick's own real selection query (`due_failed_rows`,
    which sees it unclaimed and hands back the ORM rows the tick would drive), the
    manual channel is then allowed to claim and enter its driver call, where the stub
    holds it, and only then is the tick's real drive entered with those pre-loaded
    rows; the manual drive is released after the tick's drive returns. Its per-row
    claim must lose, so it must drive nothing and report `in_progress`.
    """
    from app.services.l1_assignment_service import due_failed_rows
    from app.services.wiring_retry_service import _reattempt_rows

    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/11",
            port_b="0/0/12",
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    # The tick's own selection, UNSCOPED and real (issue #819: this call bypasses
    # run_wiring_retry_tick, and so the scoping patch in _patches, on purpose, to
    # prove the claim exclusion on the real query directly). On a used gate ledger
    # this can return other reservations' unclaimed FAILED rows too, so assert this
    # test's row is somewhere in that real result and unclaimed, not that it is the
    # only row, and drive only this test's own preselected row forward.
    async with session_factory() as db:
        # SCOPED_SELECT_LIMIT, not the production batch size: created_at ascending
        # means older foreign rows sort first, so a small limit could push this test's
        # own row out of the result on a gate ledger holding more due rows than that.
        preselected = await due_failed_rows(db, SCOPED_SELECT_LIMIT, TEST_TICK_CAP)
    preselected_by_id = {r.id: r for r in preselected}
    assert row_id in preselected_by_id, (
        "the background channel must have loaded the row while it was still unclaimed"
    )
    own_row = preselected_by_id[row_id]
    assert own_row.claimed_until is None, "the row must have been unclaimed at selection time"

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        # The manual channel's claim is committed before its driver call, so once the
        # stub reports the drive entered the interleave under test is the intended one.
        # The background channel then reaches its driver call with ONLY the row it
        # itself preselected for this test, never any foreign row the same real query
        # might have also returned (issue #819).
        manual_result, outcomes = await _loser_inside_holders_drive(
            reattempt_reservation(reservation_id, _session_ctx(session_factory)),
            lambda: _reattempt_rows([own_row], _session_ctx(session_factory)),
            state,
            entered,
            release,
        )
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert [o["outcome"] for o in outcomes] == ["in_progress"], (
        "a row claimed since selection must report in_progress, never still_failed"
    )
    assert _row_calls(calls) == ["disconnect_ports"], (
        f"exactly one driver call for the row, got {_row_calls(calls)}"
    )
    assert [r["outcome"] for r in manual_result["results"]] == ["released"], (
        "the claim holder drove the row and reported the real outcome"
    )
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before, (
        "a scoped tick must never change another reservation's ledger rows"
    )


# Issues #1473 and #1479: a retry pass that loaded a row before another pass claimed
# it must not rewrite that row under the holder's drive.


async def _seed_failed_l1_release(session_factory, reservation_id, port_a, port_b):
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a=port_a,
            port_b=port_b,
            status="FAILED",
            intended="RELEASED",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        return row.id


async def _preselect_own_row(session_factory, row_id):
    """The tick's real selection, run before any claim exists (see the CAS test above)."""
    from app.services.l1_assignment_service import due_failed_rows

    async with session_factory() as db:
        preselected = await due_failed_rows(db, SCOPED_SELECT_LIMIT, TEST_TICK_CAP)
    by_id = {r.id: r for r in preselected}
    assert row_id in by_id, "the early pass must have loaded the row while it was unclaimed"
    assert by_id[row_id].claimed_until is None
    return by_id[row_id]


@pytest.mark.asyncio
async def test_a_load_failure_from_an_early_pass_must_not_rewrite_a_held_row(session_factory):
    """Issue #1473: a held row must still read `in_progress` to the manual channel.

    Three passes, ordered by explicit events, no sleeps. Pass B (a second tick, for
    example another replica or the gate stack's own execution service) selects the row
    while it is free. Pass A (this test's tick) then claims it and enters its drive,
    where the stub holds it. B now reaches its apply and cannot load the switch (404,
    so `recorded hop unresolvable: switch ... not found`, a pinned non-retryable
    reason). B's failure write is the issue #814 row-identity compare-and-swap, whose
    predicate is `id AND status = 'FAILED'` only: it does not consult the claim, so it
    overwrites `last_error` and clears `claimed_until` on a row A still holds. The
    manual call made while A still holds the drive then reports `not_retryable` (it
    classifies by `last_error` before it looks at the claim) instead of `in_progress`.
    """
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_rows

    reservation_id = uuid.uuid4()
    row_id = await _seed_failed_l1_release(session_factory, reservation_id, "0/0/21", "0/0/22")
    early_row = await _preselect_own_row(session_factory, row_id)

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    holder = None
    try:
        holder = asyncio.create_task(run_wiring_retry_tick(_session_ctx(session_factory)))
        await _wait_entered(entered)

        # Pass B: the switch is gone for it (only B fetches from here on; A fetched
        # before it entered the drive).
        with patch("app.services.nats_consumer._fetch_device", new=AsyncMock(return_value=None)):
            await _reattempt_rows(
                [early_row], _session_ctx(session_factory), WiringRowClaims(), set()
            )

        async with session_factory() as db:
            mid = await db.get(L1ConnectionAssignment, row_id)
            mid_last_error, mid_claimed_until = mid.last_error, mid.claimed_until

        manual_result = await reattempt_reservation(reservation_id, _session_ctx(session_factory))
        holder_still_driving = not release.is_set() and not holder.done()
    finally:
        release.set()
        if holder is not None:
            await holder
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert not state["release_timed_out"]
    assert holder_still_driving, "the manual call must have run inside the holder's drive"
    outcomes = {r["id"]: r["outcome"] for r in manual_result["results"]}
    assert outcomes == {str(row_id): "in_progress"}, (
        "the manual call made while the holder drives must REPORT the row as in_progress; "
        "the row it read had been rewritten by a pass that never claimed it: "
        f"last_error={mid_last_error!r}, claimed_until={mid_claimed_until!r}"
    )
    assert mid_claimed_until is not None and mid_last_error == "driver timeout"


@pytest.mark.asyncio
async def test_a_login_failure_from_an_early_pass_must_not_free_a_held_row(session_factory):
    """Issue #1473, the double-drive consequence of the same write.

    As above, but pass B's switch loads and its LOGIN fails (a retryable reason). B's
    failure write clears `claimed_until` on the row A is still driving, so the row is
    claimable again: the manual call made while A still holds the drive claims it and
    calls the driver a SECOND time for the same row, the exact thing issue #817's
    claim exists to forbid.
    """
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_rows

    reservation_id = uuid.uuid4()
    row_id = await _seed_failed_l1_release(session_factory, reservation_id, "0/0/23", "0/0/24")
    early_row = await _preselect_own_row(session_factory, row_id)

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    holder = None
    try:
        holder = asyncio.create_task(run_wiring_retry_tick(_session_ctx(session_factory)))
        await _wait_entered(entered)

        state["fail_logins"] = True
        await _reattempt_rows([early_row], _session_ctx(session_factory), WiringRowClaims(), set())
        state["fail_logins"] = False

        manual_result = await reattempt_reservation(reservation_id, _session_ctx(session_factory))
        holder_still_driving = not release.is_set() and not holder.done()
    finally:
        release.set()
        if holder is not None:
            await holder
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert not state["release_timed_out"]
    assert holder_still_driving, "the manual call must have run inside the holder's drive"
    outcomes = {r["id"]: r["outcome"] for r in manual_result["results"]}
    assert _row_calls(calls) == ["disconnect_ports"], (
        "a row the holder is still driving reached the driver again: "
        f"{_row_calls(calls)}, manual outcome {outcomes}"
    )
    assert outcomes == {str(row_id): "in_progress"}
