"""Postgres-live proof that an empty pending fork-prune marker is SQL NULL (issue #1522).

`reservations.pending_fork_prune_device_ids` is a `json` column. Declared as plain
`JSON`, SQLAlchemy stored a Python None (an explicit None at insert, and every clear's
`remaining or None`) as the JSON literal "null", which matches the pending-prune
reconciler's `IS NOT NULL` filter; the reconciler then kept selecting every cleared row
and starved new markers once 20 existed. The model now declares
`JSON(none_as_null=True)` and migration 0018 rewrites the rows stored before. The
SQLite suites (test_pending_fork_prune.py, test_pending_fork_prune_sql_null_migration.py)
pin both; this suite proves them on the real column type and on the migration's
Postgres branch (a jsonb comparison):

  - every ORM write that empties or never sets the marker stores SQL NULL, a partial
    clear stores the rest as a list, and the reconciler's real statement selects
    exactly the rows that still hold a list;
  - migration 0018's upgrade, schema-qualified as the stack runs it, turns JSON "null"
    and an empty list (whatever their spacing) into SQL NULL, leaves SQL NULL and a
    non-empty list byte for byte, and is a no-op the second time.

Env contract identical to test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database while its reservations service's sweeper runs. Every test works inside ONE
transaction on one connection, at REPEATABLE READ, and rolls it back: its own rows
(a random user_id) are never committed, so the live sweeper never sees them and nothing
needs deleting, and the production clear's own sessions join that transaction through
savepoints. Inside the transaction every foreign row's marker is compared before and
after: the ORM test must leave all of them as they were, and the migration test must
change exactly the foreign rows holding an empty form (to SQL NULL; a database built by
this code holds none, a database written before it may). The migration's UPDATE holds
row locks on those rows until the rollback, a few milliseconds. After the rollback the
committed markers of foreign rows are compared with a snapshot taken before the test;
rows holding a non-empty list are left out of that comparison only, since the live
sweeper may legitimately converge one meanwhile.

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
from app.services.reservation_service import _clear_pending_fork_prune
from app.tasks.expiration import _pending_prune_stmt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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

SQL_NULL = "SQL NULL"
_EMPTY_FORMS_SQL = (
    "pending_fork_prune_device_ids IS NOT NULL"
    " AND pending_fork_prune_device_ids::jsonb IN ('null'::jsonb, '[]'::jsonb)"
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
    """Committed foreign markers, rows holding a non-empty list left out (the live
    sweeper may converge one between two snapshots)."""
    async with pg_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, pending_fork_prune_device_ids IS NULL AS is_null,"
                " pending_fork_prune_device_ids::text AS raw FROM reservations"
                " WHERE user_id <> :u AND (pending_fork_prune_device_ids IS NULL"
                f" OR ({_EMPTY_FORMS_SQL}))"
            ),
            {"u": user_id},
        )
        return {r.id: SQL_NULL if r.is_null else r.raw for r in rows}


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


async def test_every_orm_emptying_write_stores_sql_null_on_postgres(pg_engine):
    user_id = uuid.uuid4()
    committed_before = await _committed_foreign(pg_engine, user_id)
    a, b, c = (str(uuid.uuid4()) for _ in range(3))

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        trans = await conn.begin()
        try:
            foreign_before = await _markers(conn, user_id, own=False)
            factory = async_sessionmaker(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
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
    assert await _committed_foreign(pg_engine, user_id) == committed_before


# (stored json text, or None for SQL NULL; expected state after the upgrade)
_MIGRATION_ROWS = [
    ("null", SQL_NULL),
    (" null ", SQL_NULL),
    ("[]", SQL_NULL),
    ("[ ]", SQL_NULL),
    (None, SQL_NULL),
    ('["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]', '["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]'),
    ('["a",  "b"]', '["a",  "b"]'),
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
            now = datetime.now(timezone.utc)
            expected: dict[uuid.UUID, str] = {}
            for stored, after in _MIGRATION_ROWS:
                rid = uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO reservations (id, user_id, topology_type, start_time,"
                        " end_time, status, pending_fork_prune_device_ids) VALUES"
                        " (:id, :u, 'PHYSICAL', :s, :e, 'ACTIVE', CAST(:m AS json))"
                    ),
                    {
                        "id": rid,
                        "u": user_id,
                        "s": now - timedelta(hours=1),
                        "e": now + timedelta(hours=2),
                        "m": stored,
                    },
                )
                expected[rid] = after
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
    assert await _committed_foreign(pg_engine, user_id) == committed_before
