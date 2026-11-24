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
  - migration 0018's upgrade, schema-qualified as the stack runs it, turns JSON "null"
    and an empty list (whatever their spacing) into SQL NULL without converting any
    row to jsonb, leaves every other row byte for byte (`["\\u0000"]`, which a
    `::jsonb` cast refuses, included), and is a no-op the second time.

Env contract identical to test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database while its reservations service's sweeper runs. Every test works inside ONE
transaction on one connection, at REPEATABLE READ, and rolls it back: its own rows
(a random user_id) are never committed, so the live sweeper never sees them and nothing
needs deleting, and the production clear's and heal's own sessions join that
transaction through savepoints. Inside the transaction (a fixed snapshot) every
foreign row's marker is compared exactly before and after: the ORM and heal tests must
leave all of them as they were, and the migration test must change exactly the foreign
rows holding an empty form (to SQL NULL; a database built by this code holds none, a
database written before it may). The migration's UPDATE holds row locks on those rows
until the rollback, a few milliseconds. After the rollback the committed markers of the
foreign rows that existed before the test are compared with a snapshot taken then, by
`tests/_prune_marker_snapshot.py`'s `foreign_marker_violations`: a row that was SQL
NULL must still be; a row holding any other non-list or empty value must be unchanged
or SQL NULL (the running sweeper's heal); a row holding a list may change (the running
sweeper may prune it); rows created or deleted meanwhile are ignored.

The engine pins search_path to the reservations schema: the models are mapped
schema-less by the SQLite-oriented conftest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
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
)
from app.tasks.expiration import _pending_prune_stmt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests._prune_marker_snapshot import SQL_NULL, foreign_marker_violations

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")

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
