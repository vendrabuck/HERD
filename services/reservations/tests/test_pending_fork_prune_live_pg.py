"""Postgres-live proof that an empty pending fork-prune marker is SQL NULL (issue #1522).

`reservations.pending_fork_prune_device_ids` is a `json` column. Declared as plain
`JSON`, SQLAlchemy stored a Python None (an explicit None at insert, and every clear's
`remaining or None`) as the JSON literal "null", which matches the pending-prune
reconciler's `IS NOT NULL` filter; the reconciler then kept selecting every cleared row
and starved new markers once 20 existed. The model now declares
`JSON(none_as_null=True)`, migration 0018 rewrites the rows stored before, and the
reconciler heals any non-actionable marker it still selects. The SQLite suites
(test_pending_fork_prune.py, test_pending_fork_prune_sql_null_migration.py) pin all
three; this suite proves them on the real column type:

  - every ORM write that empties or never sets the marker stores SQL NULL, a partial
    clear stores the rest as a list, and the reconciler's real statement selects
    exactly the rows that still hold a list;
  - the heal stores SQL NULL for a JSON "null" (which loads as None, so an attribute
    assignment of None would write nothing) and for a corrupt non-list value, and
    leaves a real list alone;
  - the heal holds the row lock from its re-read to its UPDATE, so a second
    connection's write of a real list in that window waits for the heal's commit
    (here it is refused by its lock_timeout) instead of being nulled;
  - migration 0018's upgrade, schema-qualified as the stack runs it, turns JSON "null"
    and an empty list (whatever their spacing) into SQL NULL without converting any
    row to jsonb, leaves every other row byte for byte (`["\\u0000"]`, which a
    `::jsonb` cast refuses, included), and is a no-op the second time.

Env contract identical to test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN of the database to use. Unset or
                            empty, the suite is skipped and connects to nothing.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns that skip into
                            a hard failure. A named database that does not answer
                            fails every test either way (herd_common.pg_live_suite).

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database while its reservations service's sweeper runs. Every test but the lock test
works inside ONE transaction on one connection, at REPEATABLE READ, and rolls it back:
its own rows (a random user_id) are never committed, so the live sweeper never sees
them and nothing needs deleting, and the production clear's and heal's own sessions
join that transaction through savepoints. The lock test
(`test_the_heal_holds_the_row_lock_from_its_re_read_to_its_update`) needs a heal that
commits and a writer on a second connection, so it commits ONE row of its own (a
COMPLETED reservation under a random id and user_id holding JSON "null"), writes no
other row, and deletes its row in a `finally`; the live sweeper may heal that row
first, which the test is built to tolerate (its docstring says how), and the heal
holds that row's lock for about one second. Inside each rolled-back transaction (a
fixed snapshot) every foreign row's marker is compared exactly before and after: the
ORM test and the other heal test must leave all of them as they were, and the
migration test must change exactly the foreign rows holding an empty form (to SQL
NULL; a database built by this code holds none, a database written before it may).
The migration's UPDATE holds row locks on those rows until the rollback, a few
milliseconds. After every test (the rollback, or the lock test's delete) the
committed markers of the foreign rows that existed before the test are compared with
a snapshot taken then, by `tests/_prune_marker_snapshot.py`'s
`foreign_marker_violations`: a row that was SQL NULL must still be; a row holding any
other non-list or empty value must be unchanged or SQL NULL (the running sweeper's
heal); a row holding a list may change (the running sweeper may prune it); rows
created or deleted meanwhile are ignored.

The engine pins search_path to the reservations schema: the models are mapped
schema-less by the SQLite-oriented conftest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.services.reservation_service import (
    _clear_pending_fork_prune,
    _heal_non_actionable_prune_marker,
    _prune_marker_actionable,
)
from app.tasks.expiration import _pending_prune_stmt
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests._prune_marker_snapshot import SQL_NULL, foreign_marker_violations

_PG = pg_live_target()
PG_DSN = _PG.dsn

_CONNECT_ARGS = {"server_settings": {"search_path": "reservations"}}

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0018_pending_fork_prune_sql_null.py"
)
_spec = importlib.util.spec_from_file_location("pending_fork_prune_sql_null", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

# Migration 0018's own Postgres predicate: no jsonb conversion, so a row such as
# `["\u0000"]` cannot make the snapshot query fail.
_EMPTY_FORMS_SQL = (
    "pending_fork_prune_device_ids IS NOT NULL AND"
    " CASE json_typeof(pending_fork_prune_device_ids)"
    " WHEN 'null' THEN true"
    " WHEN 'array' THEN json_array_length(pending_fork_prune_device_ids) = 0"
    " ELSE false END"
)


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    yield engine
    await engine.dispose()


async def _markers(conn, user_id: uuid.UUID, *, own: bool) -> dict[uuid.UUID, str]:
    """Every row's marker as stored: SQL_NULL, or the raw JSON text."""
    op = "=" if own else "<>"
    rows = await conn.execute(
        text(
            "SELECT id, pending_fork_prune_device_ids IS NULL AS is_null,"
            " pending_fork_prune_device_ids::text AS raw"
            f" FROM reservations WHERE user_id {op} :u"
        ),
        {"u": user_id},
    )
    return {r.id: SQL_NULL if r.is_null else r.raw for r in rows}


async def _empty_form_ids(conn, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await conn.execute(
        text(f"SELECT id FROM reservations WHERE user_id <> :u AND {_EMPTY_FORMS_SQL}"),
        {"u": user_id},
    )
    return {r.id for r in rows}


async def _committed_foreign(pg_engine, user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async with pg_engine.connect() as conn:
        return await _markers(conn, user_id, own=False)


async def _assert_committed_foreign_as_expected(pg_engine, user_id, before) -> None:
    after = await _committed_foreign(pg_engine, user_id)
    assert foreign_marker_violations(before, after) == []


async def _selected_own(conn, own_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The reconciler's real statement, narrowed to this test's rows (its WHERE is
    ANDed with the id filter before the LIMIT applies)."""
    rows = await conn.execute(_pending_prune_stmt().where(Reservation.id.in_(own_ids)))
    return {r.id for r in rows}


def _reservation(user_id: uuid.UUID, **marker) -> Reservation:
    now = datetime.now(timezone.utc)
    return Reservation(
        id=uuid.uuid4(),
        user_id=user_id,
        device_ids=[],
        topology_type=TopologyType.PHYSICAL,
        purpose="live pg prune marker",
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=2),
        status=ReservationStatus.ACTIVE,
        **marker,
    )


async def _insert_raw(conn, user_id: uuid.UUID, stored: str | None, status: str = "ACTIVE"):
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await conn.execute(
        text(
            "INSERT INTO reservations (id, user_id, topology_type, start_time,"
            " end_time, status, pending_fork_prune_device_ids) VALUES"
            " (:id, :u, 'PHYSICAL', :s, :e, CAST(:st AS reservationstatus),"
            " CAST(:m AS json))"
        ),
        {
            "id": rid,
            "u": user_id,
            "s": now - timedelta(hours=1),
            "e": now + timedelta(hours=2),
            "st": status,
            "m": stored,
        },
    )
    return rid


def _joined_factory(conn):
    return async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


async def test_every_orm_emptying_write_stores_sql_null_on_postgres(pg_engine):
    user_id = uuid.uuid4()
    committed_before = await _committed_foreign(pg_engine, user_id)
    a, b, c = (str(uuid.uuid4()) for _ in range(3))

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        trans = await conn.begin()
        try:
            foreign_before = await _markers(conn, user_id, own=False)
            factory = _joined_factory(conn)
            omitted = _reservation(user_id)
            explicit_none = _reservation(user_id, pending_fork_prune_device_ids=None)
            one_clear = _reservation(user_id, pending_fork_prune_device_ids=[a])
            two_clears = _reservation(user_id, pending_fork_prune_device_ids=[b, c])
            async with factory() as s:
                s.add_all([omitted, explicit_none, one_clear, two_clears])
                await s.commit()
            own = [omitted.id, explicit_none.id, one_clear.id, two_clears.id]

            with patch("app.services.reservation_service.AsyncSessionLocal", new=factory):
                await _clear_pending_fork_prune(one_clear.id, [a])
                await _clear_pending_fork_prune(two_clears.id, [b])
                partial = await _markers(conn, user_id, own=True)
                partial_selected = await _selected_own(conn, own)
                await _clear_pending_fork_prune(two_clears.id, [c])

            assert partial[two_clears.id].replace(" ", "") == f'["{c}"]'
            assert partial_selected == {two_clears.id}
            assert await _markers(conn, user_id, own=True) == dict.fromkeys(own, SQL_NULL)
            assert await _selected_own(conn, own) == set()
            assert await _markers(conn, user_id, own=False) == foreign_before
        finally:
            await trans.rollback()

    async with pg_engine.connect() as conn:
        assert await _markers(conn, user_id, own=True) == {}
    await _assert_committed_foreign_as_expected(pg_engine, user_id, committed_before)


async def test_the_heal_stores_sql_null_for_json_null_and_a_corrupt_marker_on_postgres(
    pg_engine,
):
    """JSON "null" loads as None: an attribute assignment of None would be no change
    and write nothing, so this is the case that proves the heal's Core UPDATE."""
    user_id = uuid.uuid4()
    committed_before = await _committed_foreign(pg_engine, user_id)
    real = f'["{uuid.uuid4()}"]'

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        trans = await conn.begin()
        try:
            foreign_before = await _markers(conn, user_id, own=False)
            json_null = await _insert_raw(conn, user_id, "null")
            terminal_null = await _insert_raw(conn, user_id, "null", status="COMPLETED")
            corrupt = await _insert_raw(conn, user_id, "5")
            listed = await _insert_raw(conn, user_id, real)
            own = [json_null, terminal_null, corrupt, listed]
            assert await _selected_own(conn, own) == set(own)

            with patch(
                "app.services.reservation_service.AsyncSessionLocal", new=_joined_factory(conn)
            ):
                healed = [await _heal_non_actionable_prune_marker(rid) for rid in own]

            assert healed == [True, True, True, False]
            assert await _markers(conn, user_id, own=True) == {
                json_null: SQL_NULL,
                terminal_null: SQL_NULL,
                corrupt: SQL_NULL,
                listed: real,
            }
            assert await _selected_own(conn, own) == {listed}
            assert await _markers(conn, user_id, own=False) == foreign_before
        finally:
            await trans.rollback()

    async with pg_engine.connect() as conn:
        assert await _markers(conn, user_id, own=True) == {}
    await _assert_committed_foreign_as_expected(pg_engine, user_id, committed_before)


# SQLSTATE lock_not_available: what a statement gets when lock_timeout expires.
_LOCK_NOT_AVAILABLE = "55P03"


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig.__cause__, "sqlstate", None)


async def _write_while_the_heal_holds_the_row(rid: uuid.UUID, marker: str) -> dict:
    """The concurrent writer: on its own engine (it runs on another thread's event
    loop), read the row's committed marker, then try to store a real list on it with
    a lock_timeout of one second. Returns what it saw and how the write ended."""
    engine = _make_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET lock_timeout = '1s'"))
            # A plain SELECT takes no row lock, so it never waits on the heal's.
            row = (
                await conn.execute(
                    text(
                        "SELECT pending_fork_prune_device_ids IS NULL AS is_null,"
                        " pending_fork_prune_device_ids::text AS raw"
                        " FROM reservations WHERE id = :id"
                    ),
                    {"id": rid},
                )
            ).one()
            await conn.commit()
            seen = SQL_NULL if row.is_null else row.raw
            try:
                await conn.execute(
                    text(
                        "UPDATE reservations SET pending_fork_prune_device_ids ="
                        " CAST(:m AS json) WHERE id = :id"
                    ),
                    {"m": marker, "id": rid},
                )
                await conn.commit()
            except DBAPIError as exc:
                await conn.rollback()
                return {"seen": seen, "write": _sqlstate(exc) or repr(exc.orig)}
            return {"seen": seen, "write": "committed"}
    finally:
        await engine.dispose()


async def test_the_heal_holds_the_row_lock_from_its_re_read_to_its_update(pg_engine):
    """A real list written between the heal's re-read and its UPDATE must not be
    nulled. The heal re-reads the marker `FOR UPDATE`, so a concurrent writer cannot
    commit in that window: it waits for the heal's commit. Without the lock the
    writer commits a real list and the heal's `IS NOT NULL` UPDATE (READ COMMITTED
    re-checks the new row version) stores SQL NULL over it, which is issue #462 again.
    `test_pending_fork_prune.py`'s
    `test_a_real_marker_written_between_the_select_and_the_heal_survives` commits its
    write before the heal starts, so the re-read alone passes it; this test needs a
    real second connection.

    Isolation. Unlike the rest of this suite, the heal must commit and the writer
    must run on its own connection, so the test COMMITS one row of its own: a
    COMPLETED reservation under a random id and user_id holding JSON "null", seeded
    with raw SQL, deleted in a `finally`. It touches no other row, and the committed
    foreign markers are compared after it exactly as in the other tests.

    Ordering, without sleeps. `_prune_marker_actionable` runs exactly once in the heal,
    after the `FOR UPDATE` re-read and before the UPDATE; the patched one runs the
    writer there, on a second engine on another thread's event loop (the heal's own
    loop is blocked meanwhile, so its transaction cannot commit and the lock is
    certainly held while the writer waits). The writer's `lock_timeout` of one second
    bounds that wait; it never orders anything.

    Beside the gate's running sweeper. That sweeper heals a JSON "null" in any status,
    so it may store SQL NULL on this row before this heal reads it. The lock does not
    depend on the value: `FOR UPDATE` locks the row whether its marker is JSON "null"
    or SQL NULL, the predicate is called either way, and the writer is refused either
    way; so the lock assertion holds in both cases and the mutant fails in both (its
    writer commits, and the heal's UPDATE then matches the real list). The writer
    records the committed marker the heal locked, and the heal must return True
    exactly when that was JSON "null". COMPLETED keeps every other sweep step off the
    row (the reminder, expiry, and fork backstop select ACTIVE rows; the fork archive
    only acts on ids cabling reports; nothing requests a purpose classification).
    """
    user_id = uuid.uuid4()
    committed_before = await _committed_foreign(pg_engine, user_id)
    real = f'["{uuid.uuid4()}"]'
    async with pg_engine.connect() as conn:
        rid = await _insert_raw(conn, user_id, "null", status="COMPLETED")
        await conn.commit()

    outcome: dict = {}

    def _actionable_then_concurrent_write(marker: object) -> bool:
        actionable = _prune_marker_actionable(marker)
        if not outcome:
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    outcome.update(
                        pool.submit(
                            asyncio.run, _write_while_the_heal_holds_the_row(rid, real)
                        ).result()
                    )
            except BaseException as exc:  # surfaced by the assertions below
                outcome["error"] = repr(exc)
        return actionable

    try:
        with (
            patch(
                "app.services.reservation_service.AsyncSessionLocal",
                new=async_sessionmaker(pg_engine, expire_on_commit=False),
            ),
            patch(
                "app.services.reservation_service._prune_marker_actionable",
                new=_actionable_then_concurrent_write,
            ),
        ):
            healed = await _heal_non_actionable_prune_marker(rid)
        async with pg_engine.connect() as conn:
            after = (await _markers(conn, user_id, own=True))[rid]
    finally:
        async with pg_engine.connect() as conn:
            await conn.execute(text("DELETE FROM reservations WHERE id = :id"), {"id": rid})
            await conn.commit()

    assert "error" not in outcome, outcome
    assert outcome["write"] == _LOCK_NOT_AVAILABLE, (
        f"the concurrent write was not refused while the heal held the row: it ended "
        f"{outcome['write']!r}, and after the heal the marker is {after!r} (a real "
        f"list, {real}, was written between the heal's re-read and its UPDATE)"
    )
    assert outcome["seen"] in ("null", SQL_NULL)
    assert healed is (outcome["seen"] == "null")
    assert after == SQL_NULL
    async with pg_engine.connect() as conn:
        assert await _markers(conn, user_id, own=True) == {}
    await _assert_committed_foreign_as_expected(pg_engine, user_id, committed_before)


# (stored json text, or None for SQL NULL; expected state after the upgrade)
_MIGRATION_ROWS = [
    ("null", SQL_NULL),
    (" null ", SQL_NULL),
    ("[]", SQL_NULL),
    ("[ ]", SQL_NULL),
    (None, SQL_NULL),
    ('["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]', '["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]'),
    ('["a",  "b"]', '["a",  "b"]'),
    # Postgres `json` stores this; a `::jsonb` cast refuses it, which would abort the
    # whole upgrade.
    ('["\\u0000"]', '["\\u0000"]'),
    # Corrupt non-list shapes are left to the reconciler's heal.
    ("{}", "{}"),
    ('""', '""'),
    ("5", "5"),
    ("true", "true"),
]


def _upgrade(sync_conn) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        _migration.upgrade()


async def test_migration_0018_rewrites_empty_markers_to_sql_null_on_postgres(
    pg_engine, monkeypatch
):
    # Qualified the way the stack runs it (DB_SCHEMA=reservations).
    monkeypatch.setattr(_migration, "_schema", "reservations")
    user_id = uuid.uuid4()
    committed_before = await _committed_foreign(pg_engine, user_id)

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        trans = await conn.begin()
        try:
            foreign_before = await _markers(conn, user_id, own=False)
            foreign_empty = await _empty_form_ids(conn, user_id)
            expected: dict[uuid.UUID, str] = {}
            for stored, after in _MIGRATION_ROWS:
                expected[await _insert_raw(conn, user_id, stored)] = after
            before = await _markers(conn, user_id, own=True)
            assert sorted(before.values()) == sorted(
                SQL_NULL if stored is None else stored for stored, _ in _MIGRATION_ROWS
            )

            await conn.run_sync(_upgrade)
            assert await _markers(conn, user_id, own=True) == expected
            foreign_expected = {
                rid: SQL_NULL if rid in foreign_empty else marker
                for rid, marker in foreign_before.items()
            }
            assert await _markers(conn, user_id, own=False) == foreign_expected

            await conn.run_sync(_upgrade)
            assert await _markers(conn, user_id, own=True) == expected
            assert await _markers(conn, user_id, own=False) == foreign_expected
        finally:
            await trans.rollback()

    async with pg_engine.connect() as conn:
        assert await _markers(conn, user_id, own=True) == {}
    await _assert_committed_foreign_as_expected(pg_engine, user_id, committed_before)
