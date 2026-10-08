"""A rolled-back rehearsal session for import dry runs (issues #1017, #1064).

An importer that commits per row can run its dry run through the SAME service
path as a committing import by handing that path a rehearsal session instead
of the request session. On the rehearsal session each `commit()` only
releases a savepoint and each `rollback()` only rolls back to it (SQLAlchemy's
`join_transaction_mode="create_savepoint"`), so a dry run executes exactly the
checks and writes a committing run executes, a later row sees an earlier
row's write as it would on commit, and the outer transaction on the caller's
connection is rolled back on exit, writing nothing.

Dialect gate: pysqlite and aiosqlite emit no BEGIN until the first DML, so a
SAVEPOINT would open (and its RELEASE commit) the outermost SQLite
transaction; on SQLite an explicit BEGIN opens the outer transaction first.
Postgres needs no gate. This is the same dialect-gate idiom as
`herd_common.advisory_lock`.

Users: inventory's device and template importers (issue #1017, where the
pattern was introduced) and cabling's topology importer (issue #1064). A new
importer with a dry run should use this helper rather than its own copy.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def rehearsal_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Yield a session joined to `db`'s connection whose commits are savepoint
    releases; the outer transaction is always rolled back on exit."""
    conn = await db.connection()
    if conn.dialect.name == "sqlite":
        raw = await conn.get_raw_connection()
        if not raw.driver_connection.in_transaction:
            await conn.exec_driver_sql("BEGIN")
    rehearsal = AsyncSession(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield rehearsal
    finally:
        await rehearsal.close()
        await db.rollback()
