"""The class of a statement the database refused, read from the driver's error alone.

A refusal is named by its SQLSTATE: the one Postgres gives through asyncpg, or, for
the SQLite the unit suites run on, the SQLSTATE its extended error code stands for.
Never from the exception's text, its arguments, or the statement: those carry the
row's values, and a name read from text would follow whatever a value spells. The
bulk importer's row reasons (INV-BULK-28) and the device create and update answers
(INV-DEV-11) both classify through ``sqlstate_of``.
"""

from sqlalchemy.exc import DBAPIError

# A unique constraint refused the row.
UNIQUE_VIOLATION = "23505"

# SQLite's extended error codes (`sqlite3.Error.sqlite_errorname`) for the SQLSTATE
# classes the callers distinguish.
SQLITE_SQLSTATES = {
    "SQLITE_CONSTRAINT_UNIQUE": "23505",
    "SQLITE_CONSTRAINT_PRIMARYKEY": "23505",
    "SQLITE_CONSTRAINT_FOREIGNKEY": "23503",
    "SQLITE_CONSTRAINT_NOTNULL": "23502",
    "SQLITE_CONSTRAINT_CHECK": "23514",
    "SQLITE_TOOBIG": "22001",
}


def sqlstate_of(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a refused statement: asyncpg's, or SQLite's error code mapped
    to the same classes. None when the driver gives neither."""
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    if isinstance(sqlstate, str):
        return sqlstate
    return SQLITE_SQLSTATES.get(getattr(orig, "sqlite_errorname", None) or "")
