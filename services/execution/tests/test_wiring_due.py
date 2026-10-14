"""The retry tick's selection predicate equals its drive rule (issue #1190).

`run_wiring_retry_tick` used to select "oldest FAILED rows under the cap, LIMIT batch"
and only then, in Python, drop the rows it would never drive: a pinned non-retryable
reason, and a build-direction row of a frozen reservation. The skip left such a row as
it was, so one batch of them at the old end of a ledger hid every newer retryable row
on every later tick. The rule now sits in the three due queries (wiring_due.tick_would_drive).

What this file pins, per ledger (L1, L2, L3), on SQLite:

  - PARITY: over the full product of reason, direction, wiring state, attempts, and
    claim (tests/_wiring_due_cases.py), the due query returns a row if and only if
    the tick's Python rule would drive it. The Postgres-live suite
    (test_wiring_retry_due_predicate_live_pg.py) runs the same cases on Postgres.
  - STARVATION: a full batch of stale rows of each kind, all older than one retryable
    row, does not hide it from the select, and one tick DRIVES it.
  - The direction-scoped freeze (issue #369): a release-direction row of a frozen
    reservation is still selected and driven.
  - The second line: a reservation frozen after the select is skipped once by the
    tick's Python check and excluded by the next select.
  - The prefixes come from the one tuple; the per-reservation FAILED readers are
    untouched; the new `excluded_*` stats count what they say and cannot fail a tick.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.vlan_assignment import VlanAssignment
from app.services import nats_consumer, wiring_due, wiring_retry_service
from app.services.nats_consumer import (
    NON_RETRYABLE_REASON_PREFIXES,
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_UNRESOLVABLE_REASON,
)
from app.services.wiring_retry_service import is_retryable_failure, run_wiring_retry_tick
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests._wiring_due_cases import (
    BUILD,
    LEDGER_IDS,
    LEDGERS,
    RELEASE,
    SWITCH_ID,
    Ledger,
    assert_parity,
    assert_reason_expectations_match_python,
    build_population,
)

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

CAP = 10
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
# Explicit, distinct created_at values: SQLite's CURRENT_TIMESTAMP ties at one second
# and an unordered select returns insertion order, so "older" must be written down.
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEWER = OLD + timedelta(days=1)

DRIVER_ID = uuid.uuid4()
SWITCH_DATA = {
    "name": "due-switch",
    "template_id": "tmpl-1",
    "driver_id": str(DRIVER_ID),
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "Template", "sections": []}
SUCCESS = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}
# The driver actions that mean a row was driven; login and logout are per switch.
ROW_ACTIONS = (
    "connect_ports",
    "disconnect_ports",
    "add_to_vlan",
    "remove_from_vlan",
    "configure_route",
    "remove_route",
)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _pin_tick_settings(monkeypatch):
    monkeypatch.setattr(settings, "wiring_retry_max_attempts", CAP)
    monkeypatch.setattr(settings, "wiring_retry_batch_size", 20)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


def _recorder():
    calls: list[str] = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        return SUCCESS

    return execute_fn, calls


@contextmanager
def _driver_patches(execute_fn):
    """Inventory, the driver load, and the sandbox; everything from the ledger down is real."""

    async def _device(device_id, client=None):
        return {**SWITCH_DATA, "id": str(device_id)}

    with ExitStack() as stack:
        for p in (
            patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
            patch(
                "app.services.nats_consumer._fetch_template",
                new=AsyncMock(return_value=TEMPLATE_DATA),
            ),
            patch(
                "app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")
            ),
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        ):
            stack.enter_context(p)
        yield


def _row_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c in ROW_ACTIONS]


async def _tick(execute_fn) -> dict:
    with _driver_patches(execute_fn):
        return await run_wiring_retry_tick(_db_session_factory())


async def _freeze(reservation_id: uuid.UUID) -> None:
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=reservation_id, frozen=True))
        await s.commit()


async def _seed(rows) -> None:
    """Insert `rows` in the order given (the caller passes newest first)."""
    async with TestSessionLocal() as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()


async def _seed_release_row(
    ledger: Ledger, reservation_id: uuid.UUID, *, created_at: datetime = NEWER, n: int = 9000
) -> uuid.UUID:
    """One retryable RELEASE-direction FAILED row the tick can drive end to end.

    Release direction on purpose (the Postgres-live claim suite's choice too): a release
    needs no cabling fork fetch and no intent revalidation, so the same small harness
    drives all three ledgers. An L2 membership needs its allocation row to resolve the
    VLAN number.
    """
    cols = {}
    async with TestSessionLocal() as s:
        if ledger.name == "l2":
            alloc = VlanAssignment(
                reservation_id=reservation_id, fabric_id=uuid.uuid4(), vlan_id=1001
            )
            s.add(alloc)
            await s.flush()
            cols["vlan_assignment_id"] = alloc.id
        if ledger.name == "l3":
            cols["device_id"] = SWITCH_ID
        row = ledger.make(
            reservation_id,
            n,
            intended=RELEASE,
            attempts=1,
            last_error="driver timeout",
            created_at=created_at,
            **cols,
        )
        s.add(row)
        await s.commit()
        return row.id


def _stale_rows(ledger: Ledger, kind: str, count: int, frozen_reservation: uuid.UUID) -> list:
    """`count` rows the tick will never drive, oldest at index 0, all older than NEWER."""
    rows = []
    for i in range(count):
        if kind == "not_retryable":
            rows.append(
                ledger.make(
                    uuid.uuid4(),
                    i,
                    intended=BUILD if i % 2 else RELEASE,
                    attempts=0,
                    last_error=f"{NON_RETRYABLE_REASON_PREFIXES[i % 2]}: switch gone",
                    created_at=OLD + timedelta(seconds=i),
                )
            )
        else:
            rows.append(
                ledger.make(
                    frozen_reservation,
                    1000 + i,
                    intended=BUILD,
                    attempts=1,
                    last_error="boom",
                    created_at=OLD + timedelta(seconds=1000 + i),
                )
            )
    return rows


async def _snapshot(ledger: Ledger) -> dict:
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(ledger.model))).scalars().all()
    return {r.id: (r.status, r.intended, r.attempts, r.last_error, r.claimed_until) for r in rows}


# --- Parity: the select equals the drive rule --------------------------------


def test_reason_cases_agree_with_is_retryable_failure():
    """The case list's written-out reason rule and the production Python rule agree,
    and a few answers are pinned as literals so neither can move silently."""
    assert_reason_expectations_match_python(tuple(NON_RETRYABLE_REASON_PREFIXES))
    assert is_retryable_failure(None) is True
    assert is_retryable_failure("") is True
    assert is_retryable_failure(f"{WIRING_UNRESOLVABLE_REASON}: switch x not found") is False
    assert is_retryable_failure(WIRING_NOT_SIMPLE_CHAIN_REASON) is False
    # str.startswith is case-sensitive, so a case variant is an ordinary driver error.
    assert is_retryable_failure(WIRING_UNRESOLVABLE_REASON.upper()) is True
    assert is_retryable_failure(WIRING_NOT_SIMPLE_CHAIN_REASON.capitalize()) is True
    assert is_retryable_failure(f"driver said: {WIRING_UNRESOLVABLE_REASON}") is True


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_due_query_returns_exactly_the_rows_the_tick_would_drive(ledger):
    """The full product of the five dimensions: returned by the due query (no LIMIT in
    the way) if and only if FAILED, under the cap, unclaimed, and driven by the rule."""
    population = build_population(ledger, tuple(NON_RETRYABLE_REASON_PREFIXES), NOW, CAP)
    await _seed([*population.state_rows, *population.rows])

    async with TestSessionLocal() as db:
        returned = {r.id for r in await ledger.due(db, 100_000, CAP, NOW)}

    assert population.expected_ids, "the case product must contain drivable rows"
    assert len(population.expected_ids) < len(population.rows)
    assert_parity(population, returned)


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
@pytest.mark.parametrize(
    "prefixes",
    [
        (),
        (*NON_RETRYABLE_REASON_PREFIXES, "a third pinned reason"),
        ("100%_", "\u00c9chec du pilote", "boom"),
        ("",),
    ],
    ids=["no_prefixes", "one_more_prefix", "wildcard_and_non_ascii_prefixes", "empty_prefix"],
)
async def test_due_query_follows_the_prefix_tuple(ledger, prefixes, monkeypatch):
    """The SQL is built from NON_RETRYABLE_REASON_PREFIXES, the tuple the Python rule
    reads: change the tuple and the select moves with it, with no second list to edit.
    Parity must hold for a tuple with a new prefix, with LIKE wildcards and non-ASCII
    text in a prefix, with no prefix at all, and with the empty prefix."""
    monkeypatch.setattr(nats_consumer, "NON_RETRYABLE_REASON_PREFIXES", prefixes)
    monkeypatch.setattr(wiring_retry_service, "_NON_RETRYABLE_PREFIXES", prefixes)
    assert_reason_expectations_match_python(prefixes)

    population = build_population(ledger, prefixes, NOW, CAP)
    await _seed([*population.state_rows, *population.rows])
    async with TestSessionLocal() as db:
        returned = {r.id for r in await ledger.due(db, 100_000, CAP, NOW)}

    assert_parity(population, returned)


def test_python_rule_and_sql_read_one_prefix_tuple():
    """One definition: the Python rule's tuple IS nats_consumer's, and the SQL helper
    reads that same object each time it builds a statement."""
    assert wiring_retry_service._NON_RETRYABLE_PREFIXES is NON_RETRYABLE_REASON_PREFIXES
    assert wiring_due._non_retryable_prefixes() == tuple(NON_RETRYABLE_REASON_PREFIXES)


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_per_reservation_failed_readers_still_return_every_failed_row(ledger):
    """The readers the manual retry and the reconcile's stale-build widening share do
    NOT take the tick's predicate: they return every FAILED row of the reservation,
    not-retryable, frozen-build, at-cap, and claimed ones included."""
    population = build_population(ledger, tuple(NON_RETRYABLE_REASON_PREFIXES), NOW, CAP)
    await _seed([*population.state_rows, *population.rows])

    for state, reservation_id in population.reservation_ids.items():
        async with TestSessionLocal() as db:
            got = {r.id for r in await ledger.failed_for_reservation(db, reservation_id)}
        want = {i for i, case in population.cases_by_id.items() if case.state == state}
        assert got == want, f"{ledger.failed_reader_name} dropped rows for state {state}"


# --- Starvation: stale rows cannot fill the batch ----------------------------


@pytest.mark.parametrize("kind", ["not_retryable", "frozen_build"])
@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_a_full_batch_of_stale_rows_does_not_hide_a_newer_retryable_row(ledger, kind):
    """A batch of rows the tick will never drive, all OLDER than one retryable row: the
    select with the batch LIMIT applied returns the retryable row and none of them."""
    batch = settings.wiring_retry_batch_size
    frozen_reservation = uuid.uuid4()
    await _freeze(frozen_reservation)
    retryable_id = await _seed_release_row(ledger, uuid.uuid4())
    # Newest first, so insertion order cannot stand in for the ORDER BY.
    await _seed(reversed(_stale_rows(ledger, kind, batch, frozen_reservation)))

    async with TestSessionLocal() as db:
        due = await ledger.due(db, batch, CAP, NOW)

    assert [r.id for r in due] == [retryable_id]


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_one_tick_drives_the_retryable_row_behind_a_batch_of_stale_rows(ledger):
    """The acceptance test of issue #1190. A batch of not-retryable rows AND a batch of
    frozen build rows sit older than one retryable row; the FIRST tick drives it (the
    driver is called for it and the row settles), and leaves every stale row as it was."""
    batch = settings.wiring_retry_batch_size
    frozen_reservation = uuid.uuid4()
    await _freeze(frozen_reservation)
    retryable_id = await _seed_release_row(ledger, uuid.uuid4())
    stale = [
        *_stale_rows(ledger, "not_retryable", batch, frozen_reservation),
        *_stale_rows(ledger, "frozen_build", batch, frozen_reservation),
    ]
    await _seed(reversed(stale))
    before = await _snapshot(ledger)

    execute_fn, calls = _recorder()
    stats = await _tick(execute_fn)

    assert _row_calls(calls) == [ledger.release_action], "the retryable row reached the driver"
    after = await _snapshot(ledger)
    assert after[retryable_id][0] == "RELEASED"
    assert {i: v for i, v in after.items() if i != retryable_id} == {
        i: v for i, v in before.items() if i != retryable_id
    }, "the tick must not change a row it does not drive"
    assert stats["rows_due"] == 1
    assert stats["rows_retried"] == 1
    assert stats["released"] == 1
    assert stats[f"{ledger.name}_rows_retried"] == 1
    assert (stats["skipped_not_retryable"], stats["skipped_frozen"]) == (0, 0)
    assert stats["excluded_not_retryable"] == batch
    assert stats["excluded_frozen"] == batch


# --- The direction-scoped freeze (issue #369) --------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_release_row_of_a_frozen_reservation_is_selected_and_driven(ledger):
    """A frozen reservation still finishes its releases: the release-direction row is
    returned by the select and driven by the tick, while a build-direction row of the
    same reservation is not."""
    reservation_id = uuid.uuid4()
    await _freeze(reservation_id)
    release_id = await _seed_release_row(ledger, reservation_id)
    build = ledger.make(reservation_id, 1, intended=BUILD, attempts=1, last_error="boom")
    await _seed([build])

    async with TestSessionLocal() as db:
        assert [r.id for r in await ledger.due(db, 20, CAP)] == [release_id]

    execute_fn, calls = _recorder()
    stats = await _tick(execute_fn)

    assert _row_calls(calls) == [ledger.release_action]
    after = await _snapshot(ledger)
    assert after[release_id][0] == "RELEASED"
    assert after[build.id][:3] == ("FAILED", BUILD, 1)
    assert stats["released"] == 1
    assert stats["excluded_frozen"] == 1


# --- The second line: frozen after the select --------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_row_frozen_after_the_select_is_skipped_once_then_excluded(ledger):
    """A reservation frozen between the select and the tick's own check: the Python
    check skips the build row (no driver call, row unchanged, counted skipped_frozen),
    and the next tick's select no longer returns it, so it took a batch slot once."""
    reservation_id = uuid.uuid4()
    row = ledger.make(reservation_id, 1, intended=BUILD, attempts=1, last_error="boom")
    await _seed([row])
    before = await _snapshot(ledger)
    real_due = getattr(ledger.service, ledger.due_name)

    async def _select_then_freeze(db, limit, max_attempts, now=None):
        rows = await real_due(db, limit, max_attempts, now)
        await _freeze(reservation_id)
        return rows

    execute_fn, calls = _recorder()
    with patch(f"app.services.wiring_retry_service.{ledger.due_name}", new=_select_then_freeze):
        first = await _tick(execute_fn)

    assert first["rows_due"] == 1, "the row was selected: it was not frozen yet"
    assert first["skipped_frozen"] == 1
    assert first["rows_retried"] == 0
    assert calls == []
    assert await _snapshot(ledger) == before

    second = await _tick(execute_fn)

    assert second["rows_due"] == 0, "the next select excludes the now-frozen build row"
    assert second["skipped_frozen"] == 0
    assert second["excluded_frozen"] == 1
    assert calls == []
    assert await _snapshot(ledger) == before


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_tick_still_refuses_rows_its_select_should_not_have_returned(ledger):
    """The tick's two Python checks stay as written: handed a not-retryable row and a
    frozen build row (by a select without the predicate), it drives neither and counts
    each under the existing skip stats."""
    reservation_id = uuid.uuid4()
    await _freeze(reservation_id)
    pinned = ledger.make(
        uuid.uuid4(), 1, intended=RELEASE, attempts=1, last_error=WIRING_UNRESOLVABLE_REASON
    )
    frozen_build = ledger.make(reservation_id, 2, intended=BUILD, attempts=1, last_error="boom")
    await _seed([pinned, frozen_build])
    before = await _snapshot(ledger)

    async def _unfiltered(db, limit, max_attempts, now=None):
        result = await db.execute(select(ledger.model).where(ledger.model.status == "FAILED"))
        return list(result.scalars().all())

    execute_fn, calls = _recorder()
    with patch(f"app.services.wiring_retry_service.{ledger.due_name}", new=_unfiltered):
        stats = await _tick(execute_fn)

    assert stats["rows_due"] == 2
    assert stats["skipped_not_retryable"] == 1
    assert stats["skipped_frozen"] == 1
    assert stats["rows_retried"] == 0
    assert calls == []
    assert await _snapshot(ledger) == before


# --- The excluded_* stats -----------------------------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_count_tick_excluded_counts_failed_under_cap_rows_by_reason(ledger):
    """Over the full case product the aggregate equals a count done in Python: FAILED,
    under-cap rows only, claimed rows included, a row that is both not retryable and a
    frozen build counted once, as not retryable (the tick's own order)."""
    population = build_population(ledger, tuple(NON_RETRYABLE_REASON_PREFIXES), NOW, CAP)
    # Rows that are not FAILED never count, whatever their reason.
    settled = [
        ledger.make(
            population.reservation_ids["frozen"],
            50_000 + i,
            status=status,
            intended=BUILD,
            attempts=0,
            last_error=WIRING_UNRESOLVABLE_REASON,
        )
        for i, status in enumerate(("RELEASED", "RELEASED"))
    ]
    await _seed([*population.state_rows, *population.rows, *settled])

    under_cap = [c for c in population.cases_by_id.values() if not c.at_cap]
    want_not_retryable = sum(1 for c in under_cap if not c.retryable)
    want_frozen = sum(1 for c in under_cap if c.retryable and c.intended != RELEASE and c.frozen)

    async with TestSessionLocal() as db:
        got = await wiring_due.count_tick_excluded(db, ledger.model, CAP)

    assert want_not_retryable and want_frozen
    assert got == (want_not_retryable, want_frozen)


async def test_tick_excluded_stats_sum_the_three_ledgers():
    """The tick reports the excluded population of all three ledgers together, and an
    empty ledger set reports zeros, not None."""
    execute_fn, _calls = _recorder()
    empty = await _tick(execute_fn)
    assert (empty["excluded_not_retryable"], empty["excluded_frozen"]) == (0, 0)

    frozen_reservation = uuid.uuid4()
    await _freeze(frozen_reservation)
    l1, l2, l3 = LEDGERS
    await _seed(
        [
            *_stale_rows(l1, "not_retryable", 1, frozen_reservation),
            *_stale_rows(l2, "not_retryable", 2, frozen_reservation),
            *_stale_rows(l3, "not_retryable", 3, frozen_reservation),
            *_stale_rows(l1, "frozen_build", 4, frozen_reservation),
            *_stale_rows(l2, "frozen_build", 5, frozen_reservation),
            *_stale_rows(l3, "frozen_build", 6, frozen_reservation),
        ]
    )

    stats = await _tick(execute_fn)

    assert stats["excluded_not_retryable"] == 1 + 2 + 3
    assert stats["excluded_frozen"] == 4 + 5 + 6
    assert stats["rows_due"] == 0


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_a_failed_exclusion_count_does_not_fail_the_tick(ledger):
    """The count is best-effort: when it raises, the tick still drives what it selected
    and reports the two stats as None (unknown), never as 0."""
    retryable_id = await _seed_release_row(ledger, uuid.uuid4())

    execute_fn, calls = _recorder()
    with patch(
        "app.services.wiring_retry_service.count_tick_excluded",
        new=AsyncMock(side_effect=RuntimeError("count failed")),
    ):
        stats = await _tick(execute_fn)

    assert _row_calls(calls) == [ledger.release_action]
    assert (await _snapshot(ledger))[retryable_id][0] == "RELEASED"
    assert stats["released"] == 1
    assert stats["excluded_not_retryable"] is None
    assert stats["excluded_frozen"] is None


def _tick_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "action", None) == "wiring_retry_tick"]


async def test_tick_log_stays_at_info_while_only_excluded_rows_exist(caplog):
    """A ledger holding only stale rows selects nothing, and must not go quiet: the
    tick line is INFO and carries the excluded counts. With nothing at all it is DEBUG."""
    execute_fn, _calls = _recorder()
    with caplog.at_level(logging.DEBUG, logger="app.services.wiring_retry_service"):
        await _tick(execute_fn)
    (quiet,) = _tick_records(caplog)
    assert quiet.levelno == logging.DEBUG

    caplog.clear()
    frozen_reservation = uuid.uuid4()
    await _freeze(frozen_reservation)
    await _seed(
        [
            *_stale_rows(LEDGERS[0], "not_retryable", 2, frozen_reservation),
            *_stale_rows(LEDGERS[2], "frozen_build", 3, frozen_reservation),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="app.services.wiring_retry_service"):
        await _tick(execute_fn)
    (record,) = _tick_records(caplog)
    assert record.levelno == logging.INFO
    assert record.rows_due == 0
    assert record.excluded_not_retryable == 2
    assert record.excluded_frozen == 3


# --- The statement's lock scope ----------------------------------------------


class _CapturingSession:
    """Records the statement a due query executes; returns no rows."""

    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Result()


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_due_statement_locks_the_ledger_table_only(ledger):
    """On Postgres the select is FOR UPDATE OF its own ledger table, SKIP LOCKED: the
    frozen test reads reservation_wiring_state in a subquery and locks nothing there.
    SQLite renders no FOR UPDATE at all, so the dialect rendering is what a unit test
    can pin; the Postgres-live suite proves which rows are locked."""
    db = _CapturingSession()
    await ledger.due(db, 20, CAP, NOW)

    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    table = ledger.model.__tablename__
    assert sql.rstrip().endswith(f"FOR UPDATE OF {table} SKIP LOCKED")
    assert "reservation_wiring_state" in sql
    assert " JOIN " not in sql
    # The lock names the Table object itself, not the mapped class. The class resolves
    # through the mapper's memoized copy of the table; a live-Postgres suite that flips
    # `__table__.schema` after SQLite tests built that copy would get
    # "FOR UPDATE OF <table> AS <table>_1", which Postgres rejects.
    assert [el is ledger.model.__table__ for el in db.statement._for_update_arg.of] == [True]
