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
service runs its own retry tick against the same ledger. The 2026-11-19 nightly
failure (a seeded row reported `not_retryable`) matches that tick selecting a row this
suite had just seeded, not finding the suite's random switch in the real inventory, and
recording the pinned non-retryable reason over the row while this suite's tick was
driving it (the claim-blind failure write fixed by issue #1479); that reading comes
from the code and the log and was not reproduced on a gate.
Every row this suite seeds now carries HIDDEN_ATTEMPTS (1,000,000), far above the
default retry cap of 10, so a stack's due selects never return it as long as the stack
runs with a cap at or below that value (`WIRING_RETRY_MAX_ATTEMPTS` is an unbounded
setting, so a stack configured above 1,000,000 would see them), and this process's tick
runs with TEST_TICK_CAP above that (the issue #1190 suite's technique). The tick of THIS
process selects at that raised cap through the real due selects, so each of its short
selects takes FOR UPDATE SKIP LOCKED on up to SCOPED_SELECT_LIMIT (10,000) FAILED rows
of the shared ledger, foreign ones included, for the length of that one select; a stack
tick that runs in that window skips those rows for that tick.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
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
# `attempts < wiring_retry_max_attempts` (10 by default, an unbounded setting), in the
# SQL of its three due selects, so a stack whose cap is at or below this value never
# loads, claims, drives, or writes these rows. This process's own tick runs with
# TEST_TICK_CAP (see _patches), which is above it. The manual channel ignores the cap
# by design, so it needs nothing.
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


def _gated_driver_stub(gate_actions=_ROW_ACTIONS):
    """A sandbox stub whose FIRST row action blocks until the test releases it.

    `gate_actions` names the actions that can be the gated one (the row actions by
    default; issue #1496 gates the per-switch `login` instead, the window between a
    pass's plan and its per-row claim).

    `entered` is set when that first row action begins: the claim holder is inside its
    drive, and its claim is committed (every layer claims before its row's first driver
    call). The stub then waits on `release`, which the test sets only after the losing
    channel has returned, so the holder cannot finish its drive before the loser has
    run, on any runner speed. Every later row action returns at once.
    `state["fail_logins"]`, when true, makes a login answer a driver failure. The stub
    runs in the worker thread `nats_consumer._run_sandbox` hands it, so its waits never
    block the event loop; `release.wait` is bounded and records an expiry in
    `state["release_timed_out"]` for the test to fail on. `state["returned"]` turns true
    only once that gated row action has finished waiting and is about to return, so a
    test that finds it true when the loser has returned knows the stub did not hold the
    holder (a stub that stopped blocking is detected, not trusted).
    """
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    state = {
        "fail_logins": False,
        "blocked": False,
        "release_timed_out": False,
        "returned": False,
    }

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action == "login" and state["fail_logins"]:
            return _LOGIN_REFUSED
        if action in gate_actions and not state["blocked"]:
            state["blocked"] = True
            entered.set()
            if not release.wait(COORDINATION_TIMEOUT):
                state["release_timed_out"] = True
            state["returned"] = True
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
    holder's gated row action had returned (or the holder had finished) before the
    loser returned, or if the stub's release wait expired.
    """
    holder = asyncio.create_task(holder_coro)
    holder_was_driving = False
    try:
        await _wait_entered(entered, holder)
        loser_result = await run_loser()
        holder_was_driving = not stub_state["returned"] and not holder.done()
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
    """Issue #1473: the three real due selects, at this process's unpatched cap (the
    default, 10, unless the environment sets another), return none of the rows this
    suite seeds, while the same selects at TEST_TICK_CAP do. It proves the hiding for
    a stack that runs that same cap; it does not prove it for a stack configured with a
    cap above HIDDEN_ATTEMPTS, and it does not run a stack's tick."""
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
    """Regression guard for issue #1479: a held row still reads `in_progress` to the
    manual channel when a pass that never claimed it fails to load its switch.

    Three passes, ordered by explicit events, no sleeps. Pass B (a second tick, for
    example another replica or the gate stack's own execution service) selects the row
    while it is free. Pass A (this test's tick) then claims it and enters its drive,
    where the stub holds it. B now reaches its apply and cannot load the switch (404,
    so `recorded hop unresolvable: switch ... not found`, a pinned non-retryable
    reason), and records that failure by row id. Before #1479 that write matched on
    `id AND status = 'FAILED'` only, so it overwrote `last_error` and cleared
    `claimed_until` on the row A still held, and the manual call made during A's drive
    answered `not_retryable` (it classifies by `last_error` before it looks at the
    claim). The write now requires the row unclaimed, so it is refused, the row keeps
    A's claim and reason, and the manual call answers `in_progress`.
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
        holder_still_driving = not release.is_set() and not state["returned"] and not holder.done()
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
    """Regression guard for issue #1479, the double-drive consequence of the same write.

    As above, but pass B's switch loads and its LOGIN fails (a retryable reason).
    Before #1479 B's failure write cleared `claimed_until` on the row A was still
    driving, so the row became claimable again and the manual call made during A's
    drive claimed it and called the driver a SECOND time for the same row, the exact
    thing issue #817's claim exists to forbid. B's write is now refused, A keeps its
    claim, and the manual call drives nothing and answers `in_progress`.
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
        holder_still_driving = not release.is_set() and not state["returned"] and not holder.done()
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


# Issue #1479 review, finding 2: whether a pass still holds a row is judged by the
# database, through the exact `claimed_until` stamp its claim wrote. That needs the
# stamp Python computed and the timestamptz Postgres stored to compare equal in SQL.


async def _seed_failed_row(session_factory, reservation_id, layer):
    async with session_factory() as db:
        if layer == "l1":
            row = L1ConnectionAssignment(
                reservation_id=reservation_id,
                switch_device_id=SWITCH_ID,
                port_a="0/0/51",
                port_b="0/0/52",
                status="FAILED",
                intended="RELEASED",
                attempts=HIDDEN_ATTEMPTS,
                last_error="driver timeout",
            )
        elif layer == "l2":
            alloc = VlanAssignment(reservation_id=reservation_id, fabric_id=uuid.uuid4(), vlan_id=7)
            db.add(alloc)
            await db.flush()
            row = L2PortAssignment(
                reservation_id=reservation_id,
                vlan_assignment_id=alloc.id,
                switch_device_id=SWITCH_ID,
                port="0/0/53",
                status="FAILED",
                intended="RELEASED",
                attempts=HIDDEN_ATTEMPTS,
                last_error="driver timeout",
            )
        else:
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
        return type(row), row.id


async def _record_failure(layer, db, reservation_id, row_id, claims):
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    if layer == "l1":
        return await record_l1_failed(
            db,
            reservation_id,
            SWITCH_ID,
            "0/0/51",
            "0/0/52",
            1,
            "again",
            intended="RELEASED",
            row_id=row_id,
            claims=claims,
        )
    if layer == "l2":
        return await record_l2_failed(
            db,
            reservation_id,
            None,
            SWITCH_ID,
            "0/0/53",
            1,
            "again",
            intended="RELEASED",
            row_id=row_id,
            claims=claims,
        )
    return await record_route_failed(
        db,
        reservation_id,
        SWITCH_ID,
        None,
        1,
        "again",
        intended="RELEASED",
        row_id=row_id,
        claims=claims,
    )


async def _stored(session_factory, model, row_id):
    async with session_factory() as db:
        row = (await db.execute(select(model).where(model.id == row_id))).scalar_one()
        return row.claimed_until, row.last_error, row.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_the_claim_stamp_round_trips_and_decides_who_holds_on_postgres(
    session_factory, layer
):
    """The stamp a claim wrote reads back equal, to the microsecond and with its time
    zone, and the SQL equality the held clause uses matches it: a holder whose claim
    was taken over is refused and leaves the new holder's stamp alone, and the new
    holder, whose stamp matches, writes."""
    from app.services.wiring_claim import WiringRowClaims

    reservation_id = uuid.uuid4()
    model, row_id = await _seed_failed_row(session_factory, reservation_id, layer)
    try:
        stale = WiringRowClaims(budget=timedelta(seconds=-60))
        async with session_factory() as db:
            assert await stale.claim(db, model, row_id, intended="RELEASED") is True
        stored, _, _ = await _stored(session_factory, model, row_id)
        assert stored == stale.held[row_id]
        assert stored.tzinfo is not None
        assert stored.microsecond == stale.held[row_id].microsecond

        current = WiringRowClaims()
        async with session_factory() as db:
            assert await current.claim(db, model, row_id, intended="RELEASED") is True
        stored, last_error, attempts = await _stored(session_factory, model, row_id)
        assert stored == current.held[row_id] != stale.held[row_id]

        # The superseded holder's failure write is refused and changes nothing.
        async with session_factory() as db:
            assert await _record_failure(layer, db, reservation_id, row_id, stale) is None
        assert await _stored(session_factory, model, row_id) == (stored, last_error, attempts)
        assert stale.lost == {row_id}

        # The current holder's stamp matches: its failure write lands and frees the row.
        async with session_factory() as db:
            assert await _record_failure(layer, db, reservation_id, row_id, current) is not None
        stored, last_error, _ = await _stored(session_factory, model, row_id)
        assert (stored, last_error) == (None, "again")
        assert current.lost == set()
    finally:
        await _cleanup(session_factory, reservation_id)


def _wires_for_pair(port_a: str, port_b: str) -> list[dict]:
    """The two recorded hops (DUT to switch:port_a, switch:port_b to DUT) of one edge."""
    edge = str(uuid.uuid4())
    return [
        {
            "device_a_id": str(uuid.uuid4()),
            "port_a": "eth0",
            "device_b_id": str(SWITCH_ID),
            "port_b": port_a,
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": edge,
        },
        {
            "device_a_id": str(SWITCH_ID),
            "port_a": port_b,
            "device_b_id": str(uuid.uuid4()),
            "port_b": "eth0",
            "layer": "L1",
            "physical_connection_id": None,
            "edge_key": edge,
        },
    ]


@pytest.mark.asyncio
async def test_a_stale_tick_cannot_park_a_row_the_manual_channel_is_driving(session_factory):
    """Issue #1479 review, finding 1, staged with events.

    The manual channel M claims a FAILED build row X and enters its connect, where the
    stub holds it. The fork is then re-saved without X's wire. A tick T that loaded X
    before M claimed it now revalidates intent, finds X gone, and would park X (FAILED
    intended RELEASED, attempts 0, claim cleared) under M's drive, handing X to a third
    pass while M still drives it. T's park must be refused: X keeps M's claim, its
    intent, attempts, and reason, and T reports X `in_progress`. M then records its
    successful connect.
    """
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_rows

    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/61",
            port_b="0/0/62",
            status="FAILED",
            intended="ACTIVE",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id
    stale_copy = await _preselect_own_row(session_factory, row_id)

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    wires = _wires_for_pair("0/0/61", "0/0/62")

    async def _intent(reservation, client=None):
        # The re-save lands while M is inside its drive: from then on X is not intended.
        return [] if entered.is_set() else wires

    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id) + [
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(side_effect=_intent),
        )
    ]

    async def _stale_tick_then_read():
        outcomes = await _reattempt_rows(
            [stale_copy], _session_ctx(session_factory), WiringRowClaims(), set()
        )
        async with session_factory() as db:
            mid = (
                await db.execute(
                    select(L1ConnectionAssignment).where(L1ConnectionAssignment.id == row_id)
                )
            ).scalar_one()
        return outcomes, (mid.status, mid.intended, mid.attempts, mid.last_error, mid.claimed_until)

    for p in patches:
        p.start()
    try:
        manual_result, (tick_outcomes, mid) = await _loser_inside_holders_drive(
            reattempt_reservation(reservation_id, _session_ctx(session_factory)),
            _stale_tick_then_read,
            state,
            entered,
            release,
        )
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert [o["outcome"] for o in tick_outcomes] == ["in_progress"]
    status, intended, attempts, last_error, claimed_until = mid
    assert (status, intended, attempts, last_error) == (
        "FAILED",
        "ACTIVE",
        HIDDEN_ATTEMPTS,
        "driver timeout",
    ), "the stale tick's park must change nothing on a row M is driving"
    assert claimed_until is not None, "M's claim must survive the stale tick"
    assert [r["outcome"] for r in manual_result["results"]] == ["reconnected"]
    assert _row_calls(calls) == ["connect_ports"]
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before


@pytest.mark.asyncio
async def test_a_park_between_a_pass_plan_and_its_claim_refuses_the_build(session_factory):
    """Issue #1496, staged with events, two passes on separate Postgres sessions.

    Pass P1 loads a FAILED build row X and revalidates it against the fork (the wire is
    still intended), then logs in to X's switch, where the stub holds it: P1 has
    planned the connect and has not yet claimed X. The owner removes the wire, and pass
    P2, which loaded X too, revalidates, finds the wire gone, and parks X (FAILED
    intended RELEASED, attempts 0, the stale reason, the claim cleared) through its
    write guard; X is unclaimed, so the park lands. P1 is released and reaches its
    claim: X no longer intends ACTIVE, so the claim is refused in the same UPDATE that
    would have stamped it, P1 makes no connect_ports call, and it reads X back
    `still_failed`. Before #1496 P1 claimed X, connected it, and recorded it ACTIVE
    intended ACTIVE. The next pass then releases X.
    """
    from app.services.nats_consumer import WIRING_STALE_BUILD_REASON
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_rows

    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/91",
            port_b="0/0/92",
            status="FAILED",
            intended="ACTIVE",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id
    p1_copy = await _preselect_own_row(session_factory, row_id)
    p2_copy = await _preselect_own_row(session_factory, row_id)

    execute_fn, calls, entered, release, state = _gated_driver_stub(gate_actions=("login",))
    wires = _wires_for_pair("0/0/91", "0/0/92")

    async def _intent(reservation, client=None):
        # The owner removes the wire while P1 is inside its login.
        return [] if entered.is_set() else wires

    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id) + [
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(side_effect=_intent),
        )
    ]
    p1_claims = WiringRowClaims()

    async def _park_then_read():
        outcomes = await _reattempt_rows(
            [p2_copy], _session_ctx(session_factory), WiringRowClaims(), set()
        )
        async with session_factory() as db:
            mid = await db.get(L1ConnectionAssignment, row_id)
            return outcomes, (mid.status, mid.intended, mid.attempts, mid.last_error)

    for p in patches:
        p.start()
    try:
        p1_outcomes, (p2_outcomes, mid) = await _loser_inside_holders_drive(
            _reattempt_rows([p1_copy], _session_ctx(session_factory), p1_claims),
            _park_then_read,
            state,
            entered,
            release,
        )
        async with session_factory() as db:
            after = await db.get(L1ConnectionAssignment, row_id)
            after_state = (
                after.status,
                after.intended,
                after.attempts,
                after.last_error,
                after.claimed_until,
            )
        p1_calls = list(calls)

        # Conservation: the next pass takes X in its true direction.
        async with session_factory() as db:
            next_copy = await db.get(L1ConnectionAssignment, row_id)
        next_outcomes = await _reattempt_rows([next_copy], _session_ctx(session_factory))
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert [o["outcome"] for o in p2_outcomes] == ["still_failed"]
    assert mid == ("FAILED", "RELEASED", 0, WIRING_STALE_BUILD_REASON), "P2's park landed"
    assert "connect_ports" not in p1_calls, f"P1 connected a parked row: {p1_calls}"
    assert _row_calls(p1_calls) == [], "P1 made no row call at all"
    assert after_state == ("FAILED", "RELEASED", 0, WIRING_STALE_BUILD_REASON, None)
    assert [o["outcome"] for o in p1_outcomes] == ["still_failed"]
    assert (p1_claims.wrong_direction, p1_claims.lost) == ({row_id}, set())
    assert [o["outcome"] for o in next_outcomes] == ["released"]
    assert _row_calls(calls) == ["disconnect_ports"]
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_the_direction_compare_and_its_frozen_exception_on_postgres(session_factory, layer):
    """Issue #1496 on real Postgres: the claim's CASE over the correlated EXISTS on
    `reservation_wiring_state`, and the same compare in the row-id failure write.

    A FAILED row that intends ACTIVE: a release claim is refused while the reservation
    is not frozen (no value changes, the expired stamp included, and the row is in
    `wrong_direction`), and a release failure write by row id is refused the same way;
    once the reservation is frozen, the release claim is granted with the exact stamp
    it wrote, and a second pass's release failure write on a fresh copy lands."""
    from datetime import datetime, timezone

    from app.services.wiring_claim import WiringRowClaims

    reservation_id = uuid.uuid4()
    model, row_id = await _seed_failed_row(session_factory, reservation_id, layer)
    expired = datetime.now(timezone.utc) - timedelta(minutes=10)
    try:
        async with session_factory() as db:
            row = await db.get(model, row_id)
            row.intended = "ACTIVE"
            row.claimed_until = expired
            await db.commit()

        def _state():
            return _stored(session_factory, model, row_id)

        before = await _state()
        refused = WiringRowClaims()
        async with session_factory() as db:
            assert await refused.claim(db, model, row_id, intended="RELEASED") is False
        assert (refused.wrong_direction, refused.lost) == ({row_id}, set())
        async with session_factory() as db:
            assert await _record_failure(layer, db, reservation_id, row_id, refused) is None
        assert await _state() == before
        async with session_factory() as db:
            assert (await db.get(model, row_id)).intended == "ACTIVE"

        async with session_factory() as db:
            db.add(ReservationWiringState(reservation_id=reservation_id, frozen=True))
            await db.commit()

        writer = WiringRowClaims()
        async with session_factory() as db:
            assert await _record_failure(layer, db, reservation_id, row_id, writer) is not None
        async with session_factory() as db:
            landed = await db.get(model, row_id)
            assert (landed.intended, landed.claimed_until) == ("RELEASED", None)
            landed.intended = "ACTIVE"
            await db.commit()

        granted = WiringRowClaims()
        async with session_factory() as db:
            assert await granted.claim(db, model, row_id, intended="RELEASED") is True
        stored, _, _ = await _state()
        assert stored == granted.held[row_id]
    finally:
        await _cleanup(session_factory, reservation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_refused_failure_write_is_rolled_back_on_postgres(session_factory, layer):
    """Issue #1496 review 2: a refused row-id failure write leaves its values as they
    were because the writer rolls the caller's session back, not because of the value
    CASE alone. On Postgres the refused UPDATE still locks the row; after the writer
    returns, the caller's session has no transaction open, another connection can lock
    the row at once, and the row reads unchanged from both sessions."""
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed
    from app.services.wiring_claim import WiringRowClaims

    reservation_id = uuid.uuid4()
    model, row_id = await _seed_failed_row(session_factory, reservation_id, layer)
    try:
        before = await _stored(session_factory, model, row_id)
        common = {"intended": "ACTIVE", "row_id": row_id, "claims": WiringRowClaims()}
        async with session_factory() as db:
            if layer == "l1":
                written = await record_l1_failed(
                    db, reservation_id, SWITCH_ID, "0/0/51", "0/0/52", 1, "again", **common
                )
            elif layer == "l2":
                written = await record_l2_failed(
                    db, reservation_id, None, SWITCH_ID, "0/0/53", 1, "again", **common
                )
            else:
                written = await record_route_failed(
                    db, reservation_id, SWITCH_ID, None, 1, "again", **common
                )
            assert written is None
            assert common["claims"].wrong_direction == {row_id}
            assert not db.in_transaction(), "the refused write left the session open"
            assert await _row_lockable_now(session_factory, model, row_id)
            row = (await db.execute(select(model).where(model.id == row_id))).scalar_one()
            assert (row.claimed_until, row.last_error, row.attempts) == before
            assert row.intended == "RELEASED"
        assert await _stored(session_factory, model, row_id) == before
    finally:
        await _cleanup(session_factory, reservation_id)


# Issue #1479: a retry holder's success and release flips stay unconditional, exactly
# as on main (any change belongs to issue #1481), so a driver action that succeeded is
# recorded even after a writer that does not claim cleared the holder's claim. These
# two pin that main behaviour on the paths a review found an earlier form of #1479
# broke.


@pytest.mark.asyncio
async def test_teardown_during_a_held_l3_provision_leaves_its_routes_recorded_for_removal(
    session_factory,
):
    """Main's behaviour, pinned, staged with events.

    A retry pass holds L3 pin X and is inside its configure of route R when the
    reservation ends: the real teardown freezes the wiring, parks X
    (TEARDOWN_PENDING_REMOVAL, which clears the claim), deprovisions it, and leaves it
    RELEASED. The holder's configure then succeeds on the switch. Its success flip is
    unconditional, so `record_route_active` runs and its own record-time freeze branch
    (issue #461, WIRE-LEDGER-4) records R FAILED intended RELEASED for the release
    channel. An earlier form of #1479 refused this flip for the cleared claim and left R
    on the switch with no ledger row.
    """
    from app.services.l1_assignment_service import freeze_reservation_wiring
    from app.services.nats_consumer import (
        _apply_l3_adjacency,
        _FetchContext,
        _teardown_from_ledgers,
    )
    from app.services.route_service import FROZEN_PROVISION_PENDING_REMOVAL
    from app.services.wiring_claim import WiringRowClaims

    reservation_id = uuid.uuid4()
    route = {"destination": "203.0.113.0/24", "next_hop": "203.0.113.1"}
    async with session_factory() as db:
        row = RouteAssignment(
            reservation_id=reservation_id,
            device_id=SWITCH_ID,
            routes=[route],
            status="FAILED",
            intended="ACTIVE",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    patches = _patches(execute_fn, reservation_id) + [
        patch("app.services.driver_loader.get_driver_metadata", new=AsyncMock(return_value={}))
    ]

    async def _held_provision():
        async with httpx.AsyncClient() as client:
            await _apply_l3_adjacency(
                str(reservation_id),
                [],
                [{"device_id": str(SWITCH_ID), "routes": [route]}],
                _FetchContext(client),
                _session_ctx(session_factory),
                retry_row_ids={str(SWITCH_ID): row_id},
                claims=WiringRowClaims(),
            )

    async def _teardown():
        async with session_factory() as db:
            await freeze_reservation_wiring(db, reservation_id)
        async with httpx.AsyncClient() as client:
            await _teardown_from_ledgers(
                str(reservation_id), _FetchContext(client), _session_ctx(session_factory)
            )

    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    for p in patches:
        p.start()
    try:
        await _loser_inside_holders_drive(_held_provision(), _teardown, state, entered, release)
        async with session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(RouteAssignment).where(
                            RouteAssignment.reservation_id == reservation_id
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["configure_route", "remove_route"]
    by_id = {r.id: r for r in rows}
    assert by_id[row_id].status == "RELEASED", "teardown released the pin it parked"
    parked = [r for r in rows if r.id != row_id]
    assert len(parked) == 1, "the holder's configure must be recorded"
    assert (parked[0].status, parked[0].intended, parked[0].last_error) == (
        "FAILED",
        "RELEASED",
        FROZEN_PROVISION_PENDING_REMOVAL,
    )
    assert [r["destination"] for r in parked[0].routes] == ["203.0.113.0/24"]
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before


@pytest.mark.asyncio
async def test_a_reconcile_failure_write_during_a_held_l1_connect_still_records_it(
    session_factory,
):
    """Main's behaviour, pinned, staged with events.

    The manual channel holds build row X and is inside its connect. The reconcile drives
    the same pair, fails, and records its failure by key, which clears X's claim. The
    holder's connect then succeeds on the switch; its success flip is unconditional, so
    X reads ACTIVE and the manual call `reconnected` (an earlier form of #1479 refused
    the flip and left X FAILED with the reconcile's reason, reading `in_progress`).
    """
    from app.services.l1_assignment_service import record_l1_failed

    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/71",
            port_b="0/0/72",
            status="FAILED",
            intended="ACTIVE",
            attempts=HIDDEN_ATTEMPTS,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls, entered, release, state = _gated_driver_stub()
    patches = _patches(execute_fn, reservation_id) + [
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=_wires_for_pair("0/0/71", "0/0/72")),
        )
    ]

    async def _reconcile_failure_then_read():
        async with session_factory() as db:
            await record_l1_failed(
                db,
                reservation_id,
                SWITCH_ID,
                "0/0/71",
                "0/0/72",
                1,
                "reconcile failure",
                intended="ACTIVE",
            )
        async with session_factory() as db:
            mid = await db.get(L1ConnectionAssignment, row_id)
            return mid.claimed_until

    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    for p in patches:
        p.start()
    try:
        manual_result, cleared = await _loser_inside_holders_drive(
            reattempt_reservation(reservation_id, _session_ctx(session_factory)),
            _reconcile_failure_then_read,
            state,
            entered,
            release,
        )
        async with session_factory() as db:
            final = await db.get(L1ConnectionAssignment, row_id)
            final_state = (final.status, final.intended, final.last_error)
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert cleared is None, "the reconcile's key-based failure write cleared the claim"
    assert final_state == ("ACTIVE", "ACTIVE", None)
    assert [r["outcome"] for r in manual_result["results"]] == ["reconnected"]
    assert _row_calls(calls) == ["connect_ports"]
    foreign_after = await _foreign_ledger_snapshot(session_factory, reservation_id)
    assert foreign_after == foreign_before


async def _row_lockable_now(session_factory, model, row_id) -> bool:
    """True iff another connection can lock the row this instant (FOR UPDATE NOWAIT)."""
    from sqlalchemy.exc import DBAPIError

    async with session_factory() as other:
        try:
            await other.execute(
                select(model.id).where(model.id == row_id).with_for_update(nowait=True)
            )
        except DBAPIError:
            return False
        finally:
            await other.rollback()
    return True


@pytest.mark.asyncio
async def test_the_record_releases_the_row_lock_when_it_writes_nothing(session_factory):
    """write_guard locks the row in the caller's session; the lock is held inside and
    released on exit even when nothing was written, with the session still open, so a
    settle never leaves the row locked for the rest of the pass."""
    from app.services.wiring_claim import WiringRowClaims, write_guard

    reservation_id = uuid.uuid4()
    row_id = await _seed_failed_l1_release(session_factory, reservation_id, "0/0/81", "0/0/82")
    try:
        async with session_factory() as db:
            async with write_guard(db, L1ConnectionAssignment, row_id, None) as ok:
                assert ok is True  # no ledger: no lock taken at all
                assert await _row_lockable_now(session_factory, L1ConnectionAssignment, row_id)
            async with write_guard(db, L1ConnectionAssignment, row_id, WiringRowClaims()) as ok:
                assert ok is True
                assert not await _row_lockable_now(session_factory, L1ConnectionAssignment, row_id)
            assert await _row_lockable_now(session_factory, L1ConnectionAssignment, row_id)
    finally:
        await _cleanup(session_factory, reservation_id)


# --- Issue #1501: a key-based build failure landing after the freeze --------------

_BUILD_ACTION = {"l1": "connect_ports", "l2": "add_to_vlan", "l3": "configure_route"}
_RELEASE_ACTION = {"l1": "disconnect_ports", "l2": "remove_from_vlan", "l3": "remove_route"}
_LEDGER = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}
_LATE_ROUTE = {"destination": "192.0.2.0/24", "next_hop": "192.0.2.1"}
_BUILD_FAILED = {"success": False, "output": None, "error": "driver timeout", "duration_ms": 1}


async def _seed_frozen_foreign(session_factory) -> uuid.UUID:
    """Another reservation, frozen, with a FAILED build row on every ledger (hidden from
    any stack tick by HIDDEN_ATTEMPTS): a frozen read that is not scoped to the row's
    own reservation, or a write that strays, shows up against it."""
    foreign = uuid.uuid4()
    async with session_factory() as db:
        db.add(ReservationWiringState(reservation_id=foreign, frozen=True))
        alloc = VlanAssignment(reservation_id=foreign, fabric_id=uuid.uuid4(), vlan_id=8)
        db.add(alloc)
        await db.flush()
        common = {
            "reservation_id": foreign,
            "status": "FAILED",
            "intended": "ACTIVE",
            "attempts": HIDDEN_ATTEMPTS,
            "last_error": "driver timeout",
        }
        db.add(
            L1ConnectionAssignment(
                switch_device_id=SWITCH_ID, port_a="0/0/61", port_b="0/0/62", **common
            )
        )
        db.add(
            L2PortAssignment(
                vlan_assignment_id=alloc.id, switch_device_id=SWITCH_ID, port="0/0/63", **common
            )
        )
        db.add(RouteAssignment(device_id=SWITCH_ID, routes=[_LATE_ROUTE], **common))
        await db.commit()
    return foreign


async def _reservation_rows(session_factory, model, reservation_id) -> list:
    async with session_factory() as db:
        rows = (
            (await db.execute(select(model).where(model.reservation_id == reservation_id)))
            .scalars()
            .all()
        )
        return [(r.id, r.status, r.intended, r.attempts) for r in rows]


async def _every_ledger_row(session_factory, reservation_id) -> list:
    """(id, status, intended, attempts) of every ledger row of one reservation."""
    out: list = []
    for model in _LEDGER.values():
        out.extend(await _reservation_rows(session_factory, model, reservation_id))
    return sorted(out, key=lambda r: str(r[0]))


async def _hide(session_factory, model, row_id) -> None:
    """Put a row this suite owns back out of any stack tick's reach (HIDDEN_ATTEMPTS)."""
    async with session_factory() as db:
        row = await db.get(model, row_id)
        row.attempts = HIDDEN_ATTEMPTS
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_build_failure_landing_after_the_freeze_and_teardown_is_released(
    session_factory, layer
):
    """Issue #1501, staged with events on a used ledger. A reconcile apply (the real
    shared apply: key-based failure writes, no claims) is inside its build's driver call
    when the reservation ends: the real freeze and the real teardown run, and the
    teardown reads no row of this reservation. The build then fails and the apply
    records the failure by key, after the freeze and after the teardown's read. The
    write reads the freeze in its own statement on Postgres and records FAILED intended
    RELEASED, attempts 0; a scoped tick then removes it and the row is RELEASED (on L2
    the emptied allocation too). Before #1501 the row was FAILED intended ACTIVE, which
    no tick selects on a frozen reservation. The other frozen reservation's rows, and
    every other foreign row, are unchanged.

    The write leaves attempts 0, so between it and `_hide` (a few milliseconds) a
    stack's own tick could select the row; on a throwaway Postgres there is no stack.
    """
    from app.services.l1_assignment_service import freeze_reservation_wiring
    from app.services.nats_consumer import (
        _apply_l2_memberships,
        _apply_l3_adjacency,
        _apply_wiring_pairs,
        _FetchContext,
        _teardown_from_ledgers,
    )

    reservation_id = uuid.uuid4()
    model = _LEDGER[layer]
    foreign = await _seed_frozen_foreign(session_factory)
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    frozen_foreign_before = await _every_ledger_row(session_factory, foreign)

    alloc_id = None
    if layer == "l2":
        async with session_factory() as db:
            alloc = VlanAssignment(
                reservation_id=reservation_id,
                fabric_id=uuid.uuid4(),
                vlan_id=9,
                switch_device_ids=[str(SWITCH_ID)],
                defined_switch_ids=[str(SWITCH_ID)],
            )
            db.add(alloc)
            await db.commit()
            alloc_id = alloc.id

    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    gate = {"timed_out": False}

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action == _BUILD_ACTION[layer]:
            entered.set()
            if not release.wait(COORDINATION_TIMEOUT):
                gate["timed_out"] = True
            return _BUILD_FAILED
        return SUCCESS

    async def _reconcile_build():
        async with httpx.AsyncClient() as client:
            ctx = _FetchContext(client)
            get_db = _session_ctx(session_factory)
            if layer == "l1":
                await _apply_wiring_pairs(
                    str(reservation_id),
                    {},
                    {str(SWITCH_ID): [("0/0/71", "0/0/72", None)]},
                    [],
                    ctx,
                    get_db,
                )
            elif layer == "l2":
                add = {
                    "switch_device_id": str(SWITCH_ID),
                    "port": "0/0/73",
                    "vlan_assignment_id": alloc_id,
                    "vlan_id": 9,
                }
                await _apply_l2_memberships(str(reservation_id), [], [add], ctx, get_db)
            else:
                await _apply_l3_adjacency(
                    str(reservation_id),
                    [],
                    [{"device_id": str(SWITCH_ID), "routes": [_LATE_ROUTE]}],
                    ctx,
                    get_db,
                )

    async def _reservation_ends():
        async with session_factory() as db:
            await freeze_reservation_wiring(db, reservation_id)
        async with httpx.AsyncClient() as client:
            await _teardown_from_ledgers(
                str(reservation_id), _FetchContext(client), _session_ctx(session_factory)
            )

    patches = _patches(execute_fn, reservation_id) + [
        patch("app.services.driver_loader.get_driver_metadata", new=AsyncMock(return_value={}))
    ]
    for p in patches:
        p.start()
    try:
        builder = asyncio.create_task(_reconcile_build())
        try:
            await _wait_entered(entered, builder)
            await _reservation_ends()
            assert await _reservation_rows(session_factory, model, reservation_id) == [], (
                "the teardown must run before the build's failure is recorded"
            )
        finally:
            release.set()
            await asyncio.wait_for(builder, COORDINATION_TIMEOUT)
        assert not gate["timed_out"], "the build's driver call was never released"

        rows = await _reservation_rows(session_factory, model, reservation_id)
        assert [r[1:] for r in rows] == [("FAILED", "RELEASED", 0)]
        await _hide(session_factory, model, rows[0][0])

        calls.clear()
        stats = await run_wiring_retry_tick(_session_ctx(session_factory))
        assert _RELEASE_ACTION[layer] in calls
        assert stats["released"] == 1
        rows = await _reservation_rows(session_factory, model, reservation_id)
        assert [r[1:3] for r in rows] == [("RELEASED", "RELEASED")]
        if layer == "l2":
            async with session_factory() as db:
                assert (await db.get(VlanAssignment, alloc_id)).status == "RELEASED"
        assert await _foreign_ledger_snapshot(session_factory, reservation_id) == foreign_before
        assert await _every_ledger_row(session_factory, foreign) == frozen_foreign_before
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)
        await _cleanup(session_factory, foreign)


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_the_frozen_read_of_a_key_based_build_failure_is_the_rows_own_on_postgres(
    session_factory, layer
):
    """Issue #1501 on Postgres: with another reservation frozen, a key-based build
    failure of a reservation that is not frozen records intended ACTIVE with the attempts
    added, through the INSERT (bound reservation id) and the UPDATE (correlated EXISTS);
    once its own reservation is frozen, the UPDATE records intended RELEASED, attempts 0."""
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    reservation_id = uuid.uuid4()
    model = _LEDGER[layer]
    foreign = await _seed_frozen_foreign(session_factory)
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    frozen_foreign_before = await _every_ledger_row(session_factory, foreign)
    alloc_id = None
    if layer == "l2":
        async with session_factory() as db:
            alloc = VlanAssignment(reservation_id=reservation_id, fabric_id=uuid.uuid4(), vlan_id=9)
            db.add(alloc)
            await db.commit()
            alloc_id = alloc.id

    async def _write(attempts):
        async with session_factory() as db:
            if layer == "l1":
                written = await record_l1_failed(
                    db,
                    reservation_id,
                    SWITCH_ID,
                    "0/0/81",
                    "0/0/82",
                    attempts,
                    "boom",
                    intended="ACTIVE",
                )
            elif layer == "l2":
                written = await record_l2_failed(
                    db,
                    reservation_id,
                    alloc_id,
                    SWITCH_ID,
                    "0/0/83",
                    attempts,
                    "boom",
                    intended="ACTIVE",
                )
            else:
                written = await record_route_failed(
                    db,
                    reservation_id,
                    SWITCH_ID,
                    [_LATE_ROUTE],
                    attempts,
                    "boom",
                    intended="ACTIVE",
                )
            return written.id, written.intended, written.attempts

    try:
        row_id, intended, attempts = await _write(HIDDEN_ATTEMPTS)
        assert (intended, attempts) == ("ACTIVE", HIDDEN_ATTEMPTS)
        assert await _write(3) == (row_id, "ACTIVE", HIDDEN_ATTEMPTS + 3)
        async with session_factory() as db:
            db.add(ReservationWiringState(reservation_id=reservation_id, frozen=True))
            await db.commit()
        assert await _write(3) == (row_id, "RELEASED", 0)
        await _hide(session_factory, model, row_id)
        assert await _foreign_ledger_snapshot(session_factory, reservation_id) == foreign_before
        assert await _every_ledger_row(session_factory, foreign) == frozen_foreign_before
    finally:
        await _cleanup(session_factory, reservation_id)
        await _cleanup(session_factory, foreign)
