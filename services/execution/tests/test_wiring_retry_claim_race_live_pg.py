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
  - starts `reattempt_reservation` and `run_wiring_retry_tick` as two REAL
    concurrent asyncio tasks over SEPARATE Postgres sessions;
  - serves them a SLOW driver stub (a blocking sleep, which the production code runs
    off the event loop through `asyncio.to_thread`, so the loser really does get
    scheduled while the winner is mid-drive);
  - asserts the driver was called EXACTLY ONCE for the row, and that the channel
    that lost the claim reported the row rather than dropping it: `in_progress` for
    the manual channel, the `in_progress` tick stat for the background one.

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
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
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

# Long enough that the loser provably runs while the winner holds the row, short
# enough to keep the suite quick. The driver stub blocks in a worker thread
# (nats_consumer._run_sandbox wraps it in asyncio.to_thread), not on the loop.
DRIVE_SECONDS = 2.0

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


def _slow_driver_stub():
    """A sandbox stub that records every action and sleeps on the row's own op."""
    calls: list[str] = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action in _ROW_ACTIONS:
            time.sleep(DRIVE_SECONDS)
        return SUCCESS

    return execute_fn, calls


def _scope_to_reservation(real_fn, reservation_id: uuid.UUID):
    """Wrap a real `due_failed_*` query so it answers for ONE reservation only.

    The real function still runs with the real arguments: the FAILED/attempts-cap
    predicate and the `FOR UPDATE SKIP LOCKED` claim exclusion are exercised exactly
    as the tick would exercise them. Only the returned rows are narrowed, after the
    fact, to this test's own reservation, so a gate ledger holding other reservations'
    FAILED rows (issue #819) never becomes a candidate for this test's tick.
    """

    async def _scoped(db, limit, max_attempts, now=None):
        rows = await real_fn(db, limit, max_attempts, now)
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


async def _race(session_factory, reservation_id):
    """Run both channels as real concurrent tasks; return (manual_result, tick_stats).

    Each channel gets its OWN session factory instance so neither shares a
    connection or a transaction with the other: the serialization under test has to
    come from the database, not from a shared session.
    """
    manual = asyncio.create_task(
        reattempt_reservation(reservation_id, _session_ctx(session_factory))
    )
    # A short head start makes the ordering deterministic enough to assert on while
    # still leaving both channels genuinely in flight: the manual channel is inside
    # its (slow) driver call by the time the tick selects.
    await asyncio.sleep(0.4)
    tick = asyncio.create_task(run_wiring_retry_tick(_session_ctx(session_factory)))
    return await asyncio.gather(manual, tick)


def _row_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c in _ROW_ACTIONS]


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
            attempts=1,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls = _slow_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id)
    finally:
        for p in patches:
            p.stop()
        await _cleanup(session_factory, reservation_id)

    assert _row_calls(calls) == ["disconnect_ports"], (
        f"the row must reach the driver exactly once across both channels, got {_row_calls(calls)}"
    )
    outcomes = {r["id"]: r["outcome"] for r in manual["results"]}
    assert outcomes == {str(row_id): "released"}, "the winner reports the real outcome"
    # With the head start the manual channel already holds the claim by the time the
    # tick selects, so the tick's own claim predicate excludes the row before its
    # drive-time compare-and-swap is even reached: nothing due, nothing driven,
    # nothing counted as failed. The drive-time loss is proved by
    # test_a_simultaneous_start_makes_the_loser_report_in_progress below. rows_due is
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
            attempts=1,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls = _slow_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id)
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
            attempts=1,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls = _slow_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual, stats = await _race(session_factory, reservation_id)
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
    """The mirror ordering: the tick gets the head start, so the MANUAL call is the
    one that loses the claim and must report `in_progress` rather than omitting the
    row (an absent row would read to the caller as one that was already fixed)."""
    reservation_id = uuid.uuid4()
    async with session_factory() as db:
        row = L1ConnectionAssignment(
            reservation_id=reservation_id,
            switch_device_id=SWITCH_ID,
            port_a="0/0/7",
            port_b="0/0/8",
            status="FAILED",
            intended="RELEASED",
            attempts=1,
            last_error="driver timeout",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    execute_fn, calls = _slow_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        tick = asyncio.create_task(run_wiring_retry_tick(_session_ctx(session_factory)))
        await asyncio.sleep(0.4)
        manual = asyncio.create_task(
            reattempt_reservation(reservation_id, _session_ctx(session_factory))
        )
        stats, manual_result = await asyncio.gather(tick, manual)
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
    manual channel is then allowed to claim and start its slow driver call, and only
    then is the tick's real drive entered with those pre-loaded rows. Its per-row
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
            attempts=1,
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
        # 10000, not the production batch size: created_at ascending means older
        # foreign rows sort first, so a small limit could push this test's own row
        # out of the result on a gate ledger holding more due rows than that.
        preselected = await due_failed_rows(db, 10000, 10)
    preselected_by_id = {r.id: r for r in preselected}
    assert row_id in preselected_by_id, (
        "the background channel must have loaded the row while it was still unclaimed"
    )
    own_row = preselected_by_id[row_id]
    assert own_row.claimed_until is None, "the row must have been unclaimed at selection time"

    execute_fn, calls = _slow_driver_stub()
    foreign_before = await _foreign_ledger_snapshot(session_factory, reservation_id)
    patches = _patches(execute_fn, reservation_id)
    for p in patches:
        p.start()
    try:
        manual = asyncio.create_task(
            reattempt_reservation(reservation_id, _session_ctx(session_factory))
        )
        # Wait for the manual channel's claim to actually land, so the interleave under
        # test is the one intended rather than whatever the scheduler happened to do.
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with session_factory() as db:
                fresh = await db.get(L1ConnectionAssignment, row_id)
                if fresh is not None and fresh.claimed_until is not None:
                    break
        else:
            pytest.fail("the manual channel never took the claim")

        # Now the background channel reaches its driver call with ONLY the row it
        # itself preselected for this test, never any foreign row the same real
        # query might have also returned (issue #819).
        outcomes = await _reattempt_rows([own_row], _session_ctx(session_factory))
        manual_result = await manual
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
