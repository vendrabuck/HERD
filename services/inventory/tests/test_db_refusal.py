"""The SQLSTATE of a refused statement is read from the driver's error alone (INV-DEV-11,
INV-BULK-28): asyncpg's code, or SQLite's extended error name mapped to the same
classes, and never the error's text."""

import sqlite3

import pytest
from app.services.db_refusal import SQLITE_SQLSTATES, UNIQUE_VIOLATION, sqlstate_of
from sqlalchemy.exc import IntegrityError


class _Orig(Exception):
    pass


def _refusal(orig: Exception) -> IntegrityError:
    return IntegrityError("INSERT INTO devices (name) VALUES (?)", {}, orig)


def test_an_asyncpg_sqlstate_is_read_as_given():
    orig = _Orig("duplicate key value violates unique constraint")
    orig.sqlstate = "23505"
    assert sqlstate_of(_refusal(orig)) == UNIQUE_VIOLATION == "23505"


@pytest.mark.parametrize(("errorname", "sqlstate"), sorted(SQLITE_SQLSTATES.items()))
def test_a_sqlite_error_name_maps_to_its_sqlstate(errorname, sqlstate):
    orig = _Orig("anything")
    orig.sqlite_errorname = errorname
    assert sqlstate_of(_refusal(orig)) == sqlstate


def test_a_real_sqlite_unique_violation_reads_23505():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (n TEXT UNIQUE)")
        conn.execute("INSERT INTO t VALUES ('a')")
        with pytest.raises(sqlite3.IntegrityError) as caught:
            conn.execute("INSERT INTO t VALUES ('a')")
    finally:
        conn.close()
    assert sqlstate_of(_refusal(caught.value)) == "23505"


@pytest.mark.parametrize(
    "text",
    [
        "UNIQUE constraint failed: devices.name",
        'duplicate key value violates unique constraint "devices_name_key" 23505',
    ],
)
def test_text_alone_names_no_class(text):
    assert sqlstate_of(_refusal(_Orig(text))) is None


def test_a_sqlstate_that_is_not_a_string_is_ignored():
    orig = _Orig("x")
    orig.sqlstate = 23505
    assert sqlstate_of(_refusal(orig)) is None
