"""Postgres-live proof of migration 0019 (issue #1543): stored purpose suggestions that
are not a usable suggestion are reset, on the real `json` column.

The SQLite suite (`test_purpose_suggestion_unusable_reset_migration.py`) pins the
rule; this one proves the part SQLite cannot: on Postgres, every operator that looks
inside a `json` value (`->`, `->>`, `json_each`) de-escapes it and fails on text such as
`"\\u0000"`, which the `json` type stores, so the migration reads each stored value back
as text and judges it in Python. Here rows holding `\\u0000` and unpaired surrogates (in
values, keys, and `top_category`; all of them reset since the #1543 review, because
those same operators made the review list fail on them), rows nested 32, 33, and 300
levels, and rows a reader accepts sit together, and the upgrade, schema-qualified as
the stack runs it, does not abort, resets exactly the unusable ones, leaves every other
row byte for byte, and is a no-op the second time.

Env contract identical to test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (issue #819). The gate runs this suite against its ALREADY-USED
database while its reservations service's purpose sweep runs. The test works inside ONE
transaction on one connection, at REPEATABLE READ (a fixed snapshot), and rolls it
back: its own rows (a random user_id) are never committed. Inside the transaction every
foreign row is compared exactly before and after: the upgrade must reset exactly the
foreign rows the same rule calls unusable (a database built by this code holds none; a
database written before it may) and leave every other one as it was. The upgrade's
UPDATE holds row locks on those rows until the rollback, a few milliseconds.

The engine pins search_path to the reservations schema: the models are mapped
schema-less by the SQLite-oriented conftest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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
    / "0019_purpose_suggestion_unusable_reset.py"
)
_spec = importlib.util.spec_from_file_location("purpose_suggestion_unusable_reset", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


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


def _strings_and_keys(value: object, depth: int = 1) -> tuple[list[str], int, bool]:
    """Every string and key in `value`, its nesting depth, and whether it holds a number
    that is not finite (recursive: the values here are a few levels deep or a few
    hundred at most)."""
    if isinstance(value, dict):
        found, deepest, bad = list(value), depth, False
        children = value.values()
    elif isinstance(value, list):
        found, deepest, bad = [], depth, False
        children = value
    else:
        if isinstance(value, str):
            return [value], depth - 1, False
        return [], depth - 1, isinstance(value, float) and not math.isfinite(value)
    for child in children:
        more, child_depth, child_bad = _strings_and_keys(child, depth + 1)
        found += more
        deepest = max(deepest, child_depth)
        bad = bad or child_bad
    return found, deepest, bad


def _usable(stored: str) -> bool:
    """RES-PURPOSE-7's rule, restated here so the test does not take its expectation
    from the migration under test: a JSON object with a string `top_category`, nested
    no deeper than 32 levels, with no number that is not finite, and no string or key
    holding a NUL or an unpaired surrogate (one that cannot be encoded as UTF-8)."""
    try:
        value = json.loads(stored)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("top_category"), str):
        return False
    texts, depth, non_finite = _strings_and_keys(value)
    if depth > 32 or non_finite:
        return False
    for string in texts:
        if "\x00" in string:
            return False
        try:
            string.encode("utf-8")
        except UnicodeEncodeError:
            return False
    return True


def _after(state: tuple) -> tuple:
    """The state the upgrade must leave: (suggestion text, suggested_at set, dismissed_at
    set, stamp, attempts). A usable suggestion, or SQL NULL with neither time, is kept;
    anything else loses its suggestion and both times."""
    stored, at, dismissed, requested, attempts = state
    if stored is not None and _usable(stored):
        return state
    return (None, False, False, requested, attempts)


async def _states(conn, user_id: uuid.UUID, *, own: bool) -> dict[uuid.UUID, tuple]:
    op = "=" if own else "<>"
    rows = await conn.execute(
        text(
            "SELECT id, purpose_suggestion::text AS s,"
            " purpose_suggested_at IS NOT NULL AS at,"
            " purpose_suggestion_dismissed_at IS NOT NULL AS dis,"
            " purpose_classify_requested_at AS req, purpose_classify_attempts AS att"
            f" FROM reservations WHERE user_id {op} :u"
        ),
        {"u": user_id},
    )
    return {r.id: (r.s, r.at, r.dis, r.req, r.att) for r in rows}


# Stored text, as Postgres `json` keeps it.
_OWN_ROWS = [
    "[1, 2]",
    '"just a string"',
    "7",
    "true",
    "null",
    "{}",
    '{"top_category": 7}',
    '{"top_category": null}',
    '{"r": "\\u0000"}',
    '["\\u0000"]',
    '{"top_category": "training"}',
    # A NUL or an unpaired surrogate in a value, a key, or top_category: reset (they
    # were kept before the #1543 review; Postgres's `->` and `->>` refuse both).
    '{"top_category": "training", "r": "\\u0000"}',
    '{"top_category": "training", "\\u0000": 1}',
    '{"top_category": "\\u0000"}',
    '{"top_category": "\\ud800"}',
    '{"top_category": "training", "d": [{"\\udc00": 1}]}',
    # Kept: a surrogate pair, a noncharacter, and another control character.
    '{"top_category": "training", "\\ud83d\\ude00": "\\uffff\\u0001"}',
    # Depth: 32 levels kept, 33 reset, 300 reset (the response models refuse it).
    '{"top_category": "t", "x": ' + "[" * 31 + "]" * 31 + "}",
    '{"top_category": "t", "x": ' + "[" * 32 + "]" * 32 + "}",
    '{"top_category": "t", "x": ' + "[" * 299 + "]" * 299 + "}",
    '{ "top_category" : "qa_regression" }',
    None,
]


async def _insert_raw(conn, user_id: uuid.UUID, stored: str | None, index: int) -> uuid.UUID:
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await conn.execute(
        text(
            "INSERT INTO reservations (id, user_id, topology_type, start_time, end_time,"
            " status, purpose_suggestion, purpose_suggested_at,"
            " purpose_suggestion_dismissed_at, purpose_classify_requested_at,"
            " purpose_classify_attempts) VALUES (:id, :u, 'PHYSICAL', :s, :e,"
            " CAST('COMPLETED' AS reservationstatus), CAST(:v AS json), :at, :dis, :req, 2)"
        ),
        {
            "id": rid,
            "u": user_id,
            "s": now - timedelta(hours=3),
            "e": now - timedelta(hours=2),
            "v": stored,
            "at": now,
            "dis": now if index % 2 == 0 else None,
            "req": now - timedelta(hours=1),
        },
    )
    return rid


def _upgrade(sync_conn) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        _migration.upgrade()


async def test_migration_0019_resets_exactly_the_unusable_suggestions_on_postgres(
    pg_engine, monkeypatch
):
    # Qualified the way the stack runs it (DB_SCHEMA=reservations).
    monkeypatch.setattr(_migration, "_schema", "reservations")
    user_id = uuid.uuid4()

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        trans = await conn.begin()
        try:
            foreign_before = await _states(conn, user_id, own=False)
            for index, stored in enumerate(_OWN_ROWS):
                await _insert_raw(conn, user_id, stored, index)
            own_before = await _states(conn, user_id, own=True)
            assert sorted(s for s, *_ in own_before.values() if s is not None) == sorted(
                s for s in _OWN_ROWS if s is not None
            )
            own_expected = {rid: _after(state) for rid, state in own_before.items()}
            foreign_expected = {rid: _after(state) for rid, state in foreign_before.items()}
            # Both outcomes are present among this test's own rows.
            assert any(own_expected[rid] == own_before[rid] for rid in own_before)
            assert any(own_expected[rid] != own_before[rid] for rid in own_before)

            await conn.run_sync(_upgrade)
            assert await _states(conn, user_id, own=True) == own_expected
            assert await _states(conn, user_id, own=False) == foreign_expected

            await conn.run_sync(_upgrade)
            assert await _states(conn, user_id, own=True) == own_expected
            assert await _states(conn, user_id, own=False) == foreign_expected
        finally:
            await trans.rollback()

    async with pg_engine.connect() as conn:
        assert await _states(conn, user_id, own=True) == {}
