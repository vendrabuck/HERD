"""Real-Postgres coverage for herd_common.advisory_lock (issue #572).

test_advisory_lock.py covers both lock styles against mocked
connections/sessions; every assertion there is about what SQL gets sent, not
whether Postgres actually honors it. This file is the missing other half:
the SQL in session_try_lock/session_unlock/xact_lock has never executed
against a real server in any test (advisory_lock.py's own no-op gate means
SQLite unit tests skip the Postgres branch entirely), so a busted query
(wrong function name, wrong argument shape, a hashtext() collision
assumption that doesn't hold) would pass every existing test and still fail
in production.

Skipped automatically when no Postgres is reachable at the configured DSN,
so the suite stays green without one. Setting HERD_TEST_PG_REQUIRED=1
disables the skip: an unreachable server then fails every test with an
explicit message, mirroring services/auth/tests/test_ldap_service_live.py's
HERD_TEST_LDAP_REQUIRED contract.

Env contract:
    HERD_TEST_PG_DSN       SQLAlchemy asyncpg DSN, e.g.
                            postgresql+asyncpg://user:pass@host:port/db
                            Defaults to a DSN matching the dev stack's
                            published postgres port (POSTGRES_PORT, default
                            5433) so a local `make up` is reachable with no
                            extra setup: see docker-compose.yml's postgres
                            service `ports: ["${POSTGRES_PORT:-5433}:5432"]`.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an
                            unreachable DSN into a hard failure instead of a
                            skip.

Any reachable Postgres works: these tests create no tables and touch no
application schema, since advisory locks are server-instance-scoped, not
schema-scoped. They pg_advisory_unlock everything they acquire so a shared
server (e.g. someone else's dev stack) is left clean.
"""

from __future__ import annotations

import os
import uuid

import pytest
from herd_common.advisory_lock import session_try_lock, session_unlock, xact_lock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")


async def _pg_reachable() -> bool:
    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _run_sync_reachable() -> bool:
    import asyncio

    return asyncio.run(_pg_reachable())


_PG_REACHABLE = _run_sync_reachable()

pytestmark = pytest.mark.skipif(
    not _PG_REQUIRED and not _PG_REACHABLE,
    reason=(
        f"No Postgres reachable at {PG_DSN!r}; set HERD_TEST_PG_DSN to point at one "
        "(e.g. the dev stack's published postgres port, or a throwaway "
        "`docker run --rm -d -e POSTGRES_USER=herd -e POSTGRES_PASSWORD=herd "
        "-e POSTGRES_DB=herd -p 5433:5432 postgres:16-alpine`) to run this suite."
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
    engine = create_async_engine(PG_DSN)
    yield engine
    await engine.dispose()


@pytest.fixture
def lock_key():
    # Unique per test so parallel/rerun test processes never collide on a
    # session-scoped advisory lock left dangling by an earlier crashed run.
    return f"herd-test-advisory-lock-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# session_try_lock / session_unlock against real Postgres.
# ---------------------------------------------------------------------------


async def test_session_try_lock_first_connection_acquires(pg_engine, lock_key):
    async with pg_engine.connect() as conn:
        acquired = await session_try_lock(conn, lock_key)
        assert acquired is True
        await conn.commit()
        await session_unlock(conn, lock_key)


async def test_session_try_lock_second_connection_same_key_is_busy(pg_engine, lock_key):
    async with pg_engine.connect() as holder:
        acquired = await session_try_lock(holder, lock_key)
        assert acquired is True
        await holder.commit()

        # A second, independent connection (a different session on
        # Postgres's terms) is exactly what _SyncSlot's "replica" branch
        # models: a different process holds the same string key.
        async with pg_engine.connect() as contender:
            acquired_again = await session_try_lock(contender, lock_key)
            assert acquired_again is False
            # Nothing to release on the contender: it never acquired.

        await session_unlock(holder, lock_key)


async def test_session_unlock_releases_so_a_second_connection_can_then_acquire(pg_engine, lock_key):
    async with pg_engine.connect() as holder:
        assert await session_try_lock(holder, lock_key) is True
        await holder.commit()

        async with pg_engine.connect() as contender:
            assert await session_try_lock(contender, lock_key) is False

        await session_unlock(holder, lock_key)

        # Released: a fresh connection can now acquire the same key.
        async with pg_engine.connect() as new_contender:
            assert await session_try_lock(new_contender, lock_key) is True
            await new_contender.commit()
            await session_unlock(new_contender, lock_key)


async def test_session_try_lock_different_keys_do_not_contend(pg_engine, lock_key):
    other_key = f"{lock_key}-other"
    async with pg_engine.connect() as first:
        assert await session_try_lock(first, lock_key) is True
        await first.commit()

        async with pg_engine.connect() as second:
            # A different string key hashes to a different lock id, so this
            # must acquire even while the first key's lock is held.
            assert await session_try_lock(second, other_key) is True
            await second.commit()
            await session_unlock(second, other_key)

        await session_unlock(first, lock_key)


# ---------------------------------------------------------------------------
# xact_lock smoke test: transaction-scoped, auto-releases on commit.
# ---------------------------------------------------------------------------


async def test_xact_lock_acquires_inside_a_transaction_and_releases_on_commit(pg_engine):
    session_factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    numeric_key = int(uuid.uuid4().int % (2**62))

    session: AsyncSession
    async with session_factory() as session:
        await xact_lock(session, numeric_key)
        # Held inside the still-open transaction: a concurrent blocking
        # acquire of the SAME key from another connection must not
        # complete until this session commits. pg_try_advisory_xact_lock
        # (the non-blocking sibling) lets us observe that without actually
        # blocking the test.
        async with pg_engine.connect() as other:
            result = await other.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": numeric_key}
            )
            assert result.scalar_one() is False
            await other.rollback()

        await session.commit()

    # Transaction ended: pg_advisory_xact_lock auto-released the key, so a
    # fresh connection can now acquire it.
    async with pg_engine.connect() as other:
        result = await other.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": numeric_key}
        )
        assert result.scalar_one() is True
        await other.rollback()
