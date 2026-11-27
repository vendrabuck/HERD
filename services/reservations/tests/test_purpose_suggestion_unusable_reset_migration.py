"""Migration 0019 (issue #1543): stored purpose suggestions that are not a usable
suggestion are reset.

Before the fix the classifier stored whatever a 200 parsed to, so a row can hold a list,
a string, a number, a boolean, the JSON literal "null", an object without a string
`top_category`, an object nested at any depth that parsed, or a NUL or an unpaired
surrogate in any string or key, and a JSON `null` answer left `purpose_suggested_at`
set with no suggestion. 0019 stores SQL NULL in the suggestion, its time, and its
dismissal for each row the classifier would not store today (RES-PURPOSE-7), and for
stored text Python cannot parse, keeps the stamp and the attempt count, and leaves
every other row alone. The
upgrade runs for real through Alembic's Operations against an in-memory SQLite
database (the shape of `test_pending_fork_prune_sql_null_migration.py`); the Postgres
run is `test_purpose_suggestion_reset_live_pg.py`. The table here has only the columns
the migration reads or writes, so a later column added to the model cannot break the
setup. The route-level proof (every reader answers after the upgrade, and the sweep
classifies the row again) is in `test_purpose_classify_trigger.py`.
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
_MIGRATION_PATH = _RES_DIR / "migrations" / "versions" / "0019_purpose_suggestion_unusable_reset.py"
_spec = importlib.util.spec_from_file_location("purpose_suggestion_unusable_reset", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

SQL_NULL = None
# Far past the JSON decoder's recursion limit on every interpreter (about 10,000 on 3.12).
_DEEP = 200_000


def _nested(lists: int) -> str:
    """An object with a string top_category whose field `x` is `lists` lists deep, so
    the object nests `lists + 1` levels."""
    return '{"top_category": "t", "x": ' + "[" * lists + "]" * lists + "}"


AT = "2026-11-20 10:00:00"
REQUESTED = "2026-11-19 10:00:00"
RESET = (SQL_NULL, SQL_NULL, SQL_NULL)

# (row id, stored suggestion text or None, suggested_at, dismissed_at, expected
# (suggestion, suggested_at, dismissed_at) after the upgrade). Every row is stamped with
# REQUESTED and holds 2 attempts; the upgrade must keep both on every row.
_ROWS = [
    # Not a JSON object (#1543).
    ("list", "[1, 2]", AT, AT, RESET),
    ("string", '"just a string"', AT, None, RESET),
    ("integer", "7", AT, None, RESET),
    ("float", "1.5", AT, None, RESET),
    ("true", "true", AT, AT, RESET),
    ("false", "false", AT, None, RESET),
    ("json-null", "null", AT, None, RESET),
    ("json-null-spaced", " null ", AT, None, RESET),
    # An object without a string top_category (the owner's rule).
    ("empty-object", "{}", AT, None, RESET),
    ("top-category-missing", '{"pass": "end"}', AT, AT, RESET),
    ("top-category-null", '{"top_category": null}', AT, None, RESET),
    ("top-category-integer", '{"top_category": 7}', AT, None, RESET),
    ("top-category-list", '{"top_category": ["training"]}', AT, None, RESET),
    ("top-category-object", '{"top_category": {"n": "training"}}', AT, None, RESET),
    # Text Python cannot parse: not JSON, an integer over the 4,300-digit limit
    # (ValueError), and nesting past the decoder's recursion limit (RecursionError, not
    # a ValueError: a judge that caught ValueError only would abort the upgrade here).
    ("not-json", "not json", AT, None, RESET),
    ("integer-over-digit-limit", '{"top_category": "t", "n": ' + "9" * 5000 + "}", AT, None, RESET),
    ("past-decoder-limit", _nested(_DEEP), AT, None, RESET),
    # The rest of RES-PURPOSE-7's rule, which the old code did not apply: nesting past 32
    # levels (the response models refuse past about 255), a number that is not finite,
    # and a NUL or an unpaired surrogate in any string or key (Postgres's `->` and `->>`
    # refuse both). Written as JSON escapes, so this file stays ASCII.
    ("past-store-bound", _nested(32), AT, None, RESET),
    ("nan", '{"top_category": "t", "p": NaN}', AT, None, RESET),
    ("nul-in-value", '{"top_category": "training", "r": "\\u0000"}', AT, AT, RESET),
    ("nul-in-key", '{"top_category": "training", "\\u0000": 1}', AT, None, RESET),
    ("nul-in-top-category", '{"top_category": "t\\u0000"}', AT, None, RESET),
    ("surrogate-in-value", '{"top_category": "t", "d": [{"c": "\\ud800"}]}', AT, None, RESET),
    ("surrogate-in-key", '{"top_category": "t", "d": [{"\\udc00": 1}]}', AT, AT, RESET),
    ("surrogate-in-top-category", '{"top_category": "\\ud800"}', AT, None, RESET),
    # The JSON null answer's residue: no suggestion, but a time (and, defensively, a
    # dismissal with no suggestion).
    ("null-residue", None, AT, None, RESET),
    ("dismissed-without-suggestion", None, None, AT, RESET),
    # Untouched: a usable suggestion (spacing, escapes, and a dismissal kept), and a row
    # with nothing at all.
    (
        "usable",
        '{"top_category": "training"}',
        AT,
        None,
        ('{"top_category": "training"}', AT, None),
    ),
    (
        "usable-dismissed",
        '{ "top_category" : "qa_regression", "x": [1] }',
        AT,
        AT,
        ('{ "top_category" : "qa_regression", "x": [1] }', AT, AT),
    ),
    ("usable-at-store-bound", _nested(31), AT, None, (_nested(31), AT, None)),
    (
        "usable-surrogate-pair-and-noncharacter",
        '{"top_category": "t", "\\ud83d\\ude00": "\\uffff\\u0001"}',
        AT,
        None,
        ('{"top_category": "t", "\\ud83d\\ude00": "\\uffff\\u0001"}', AT, None),
    ),
    (
        "usable-duplicate-key-last-wins",
        '{"top_category": 7, "top_category": "training"}',
        AT,
        None,
        ('{"top_category": 7, "top_category": "training"}', AT, None),
    ),
    ("nothing", None, None, None, RESET),
]

_COLUMNS = (
    "id TEXT PRIMARY KEY, purpose_suggestion JSON, purpose_suggested_at TEXT,"
    " purpose_suggestion_dismissed_at TEXT, purpose_classify_requested_at TEXT,"
    " purpose_classify_attempts INTEGER NOT NULL"
)


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
    """One `reservations` table seeded with _ROWS. With `schema`, the table lives in an
    attached database of that name AND a decoy `main.reservations` holds the same rows:
    SQLite resolves an unqualified name in `main` first, so an upgrade that forgot the
    schema would rewrite the decoy instead."""
    engine = sa.create_engine("sqlite://")
    prefixes = ["main.", f"{schema}."] if schema else [""]
    with engine.begin() as conn:
        if schema:
            conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        for prefix in prefixes:
            conn.exec_driver_sql(f"CREATE TABLE {prefix}reservations ({_COLUMNS})")
            for row_id, stored, at, dismissed, _expected in _ROWS:
                conn.execute(
                    sa.text(f"INSERT INTO {prefix}reservations VALUES (:id, :s, :at, :d, :r, 2)"),
                    {"id": row_id, "s": stored, "at": at, "d": dismissed, "r": REQUESTED},
                )
    return engine


def _states(engine: sa.Engine, schema: str | None = None) -> dict[str, tuple]:
    # Read as text: a column declared JSON has numeric affinity in SQLite, so "7" is
    # stored as the integer 7.
    prefix = f"{schema}." if schema else ""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, CAST(purpose_suggestion AS TEXT), purpose_suggested_at,"
                " purpose_suggestion_dismissed_at, purpose_classify_requested_at,"
                f" purpose_classify_attempts FROM {prefix}reservations"
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _run(engine: sa.Engine, step) -> None:
    # The engine's single pooled connection keeps an ATTACHed schema across calls.
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


_EXPECTED = {row_id: (*expected, REQUESTED, 2) for row_id, _s, _a, _d, expected in _ROWS}
_BEFORE = {
    row_id: (stored, at, dismissed, REQUESTED, 2) for row_id, stored, at, dismissed, _ in _ROWS
}


def test_upgrade_resets_exactly_the_unusable_rows_and_runs_twice(make_engine):
    engine = make_engine()
    assert _states(engine) == _BEFORE

    _run(engine, _migration.upgrade)
    assert _states(engine) == _EXPECTED

    _run(engine, _migration.upgrade)
    assert _states(engine) == _EXPECTED


def test_upgrade_resets_every_unusable_row_across_batches(make_engine, monkeypatch):
    """The reset by id runs in batches; with a batch of 2 every unusable row is still
    reset (a loop that wrote only the first batch, or skipped the last partial one,
    fails here)."""
    monkeypatch.setattr(_migration, "_BATCH", 2)
    engine = make_engine()

    _run(engine, _migration.upgrade)

    assert _states(engine) == _EXPECTED


def test_upgrade_honours_db_schema(monkeypatch, make_engine):
    """The chain qualifies the table with DB_SCHEMA (read at import as `_schema`), as
    the stack runs it; an attached SQLite database stands in for the schema, and a
    decoy `main.reservations` with the same rows must come through untouched."""
    monkeypatch.setattr(_migration, "_schema", "reservations")
    engine = make_engine(schema="reservations")
    assert _states(engine, schema="main") == _BEFORE

    _run(engine, _migration.upgrade)

    assert _states(engine, schema="reservations") == _EXPECTED
    assert _states(engine, schema="main") == _BEFORE


def test_downgrade_is_a_no_op(make_engine):
    engine = make_engine()
    _run(engine, _migration.upgrade)

    _run(engine, _migration.downgrade)

    assert _states(engine) == _EXPECTED


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_upgrade_refuses_offline_mode_and_emits_nothing(dialect):
    """The upgrade reads before it writes, so offline (`--sql`) mode is refused with a
    fixed message rather than emitting SQL that would skip the repair."""
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(ctx), pytest.raises(RuntimeError) as raised:
        _migration.upgrade()
    assert str(raised.value) == (
        "Revision 0019 reads stored purpose suggestions before it resets them; "
        "run it against a live database, not in offline (--sql) mode."
    )
    assert buf.getvalue() == ""


def test_reservations_chain_is_linear_and_0019_revises_0018():
    cfg = Config(str(_RES_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_RES_DIR / "migrations"))
    # An unset path_separator is a DeprecationWarning, which
    # filterwarnings = ["error"] turns into a failure.
    cfg.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0019"]
    assert script.get_revision("0019").down_revision == "0018"
