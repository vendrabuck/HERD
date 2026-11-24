"""Migration 0018 (issue #1522): empty pending fork-prune markers become SQL NULL.

Before the model declared `JSON(none_as_null=True)`, every cleared marker was stored
as the JSON literal "null", which the pending-prune reconciler's `IS NOT NULL` filter
keeps selecting. 0018 rewrites the empty forms (JSON "null" and an empty list) to SQL
NULL and leaves every other row alone. The upgrade runs for real through Alembic's
Operations against an in-memory SQLite database (the same shape as auth's migration
tests); the Postgres branch is proven on a real server by
test_pending_fork_prune_live_pg.py. The table here has only the two columns the
migration reads, so a later column added to the model cannot break the setup.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

_RES_DIR = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _RES_DIR / "migrations" / "versions" / "0018_pending_fork_prune_sql_null.py"
_spec = importlib.util.spec_from_file_location("pending_fork_prune_sql_null", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

SQL_NULL = "SQL NULL"

# (row id, stored text or None for SQL NULL, expected state after the upgrade)
_ROWS = [
    ("json-null", "null", SQL_NULL),
    ("json-null-spaced", " null ", SQL_NULL),
    ("empty-list", "[]", SQL_NULL),
    ("empty-list-spaced", "[ ]", SQL_NULL),
    ("sql-null", None, SQL_NULL),
    (
        "one-id",
        '["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]',
        '["2d1f6c1e-0d7c-4b5e-9a51-0f3c1b2a4d5e"]',
    ),
    ("two-ids", '["a",  "b"]', '["a",  "b"]'),
]


@pytest.fixture
def make_engine():
    """Build seeded engines and dispose of them at teardown (an undisposed SQLite
    connection is a ResourceWarning, which filterwarnings = ["error"] fails on)."""
    engines: list[sa.Engine] = []

    def _make(schema: str | None = None) -> sa.Engine:
        engines.append(_engine(schema))
        return engines[-1]

    yield _make
    for engine in engines:
        engine.dispose()


def _engine(schema: str | None = None) -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    prefix = f"{schema}." if schema else ""
    with engine.begin() as conn:
        if schema:
            conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        conn.exec_driver_sql(
            f"CREATE TABLE {prefix}reservations "
            "(id TEXT PRIMARY KEY, pending_fork_prune_device_ids JSON)"
        )
        for row_id, stored, _expected in _ROWS:
            conn.execute(
                sa.text(
                    f"INSERT INTO {prefix}reservations (id, pending_fork_prune_device_ids)"
                    " VALUES (:id, :v)"
                ),
                {"id": row_id, "v": stored},
            )
    return engine


def _states(engine: sa.Engine, schema: str | None = None) -> dict[str, str]:
    prefix = f"{schema}." if schema else ""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, pending_fork_prune_device_ids IS NULL, pending_fork_prune_device_ids"
                f" FROM {prefix}reservations"
            )
        ).all()
    return {row_id: SQL_NULL if is_null else raw for row_id, is_null, raw in rows}


def _run(engine: sa.Engine, step) -> None:
    # The engine's single pooled connection keeps an ATTACHed schema across calls.
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


_EXPECTED = {row_id: expected for row_id, _stored, expected in _ROWS}
_BEFORE = {row_id: SQL_NULL if stored is None else stored for row_id, stored, _ in _ROWS}


def test_upgrade_leaves_exactly_sql_null_or_a_non_empty_list_and_runs_twice(make_engine):
    engine = make_engine()
    assert _states(engine) == _BEFORE

    _run(engine, _migration.upgrade)
    assert _states(engine) == _EXPECTED

    _run(engine, _migration.upgrade)
    assert _states(engine) == _EXPECTED


def test_upgrade_honours_db_schema(monkeypatch, make_engine):
    """The chain qualifies the table with DB_SCHEMA (read at import as `_schema`), as
    the stack runs it; an attached SQLite database stands in for the schema."""
    monkeypatch.setattr(_migration, "_schema", "reservations")
    engine = make_engine(schema="reservations")

    _run(engine, _migration.upgrade)

    assert _states(engine, schema="reservations") == _EXPECTED


def test_downgrade_is_a_no_op(make_engine):
    engine = make_engine()
    _run(engine, _migration.upgrade)

    _run(engine, _migration.downgrade)

    assert _states(engine) == _EXPECTED


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_upgrade_sql_per_dialect_touches_only_the_marker_column(dialect):
    """Offline SQL of the upgrade: one UPDATE that sets only the marker to NULL, only
    where it is not already NULL, matched by JSON value on Postgres."""
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(ctx):
        _migration.upgrade()
    statements = [s.strip() for s in buf.getvalue().split(";") if s.strip()]

    assert len(statements) == 1
    sql = statements[0]
    assert sql.startswith("UPDATE reservations SET pending_fork_prune_device_ids = NULL WHERE ")
    assert "pending_fork_prune_device_ids IS NOT NULL" in sql
    if dialect == "postgresql":
        assert "::jsonb IN ('null'::jsonb, '[]'::jsonb)" in sql


def test_reservations_chain_is_linear_and_0018_revises_0017():
    cfg = Config(str(_RES_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_RES_DIR / "migrations"))
    # An unset path_separator is a DeprecationWarning, which
    # filterwarnings = ["error"] turns into a failure.
    cfg.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(cfg)

    assert len(script.get_heads()) == 1
    on_disk = {p.stem.split("_", 1)[0] for p in (_RES_DIR / "migrations" / "versions").glob("*.py")}
    assert {r.revision for r in script.walk_revisions()} == on_disk
    assert script.get_revision("0018").down_revision == "0017"
