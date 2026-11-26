"""Real-Postgres coverage for _SyncSlot's cross-replica advisory-lock branch
(ADR 0011 phase 3/5, issue #572).

Every existing _SyncSlot test (test_ldap_sync_service.py,
test_ldap_sync_loop.py, test_ldap_sync_stale_run_reaper.py) runs on an
in-memory SQLite engine. herd_common.advisory_lock.is_postgres_dialect
no-ops the ENTIRE Postgres block in _SyncSlot.__aenter__
(ldap_sync_service.py's "Cross-replica layer" branch) on any non-Postgres
dialect, so on SQLite that block never runs at all: database.engine.connect()
is never called, session_try_lock's pg_try_advisory_lock SQL never executes,
and SyncBusyError("replica") is never raised anywhere in the existing suite
(grep confirms both busy-path tests assert reason == "in_process", the
asyncio-lock layer, not this one).

This file closes that gap by monkeypatching app.database's module-level
`engine` attribute (the same seam services/auth/tests/conftest.py already
uses for the stale-run reaper's session factory) to point at a real Postgres
DSN. ldap_sync_service reads `database.engine` fresh on every _SyncSlot
entry (`from app import database` then `database.engine.dialect.name` /
`database.engine.connect()` at call time, not import time), so no production
code changes are needed: this is a pure test-side seam.

Skipped, connecting to nothing, unless HERD_TEST_PG_DSN names a database, and
skipped too when the named database does not answer; HERD_TEST_PG_REQUIRED=1
turns either skip into an error at setup, mirroring test_ldap_service_live.py's
HERD_TEST_LDAP_REQUIRED contract. See
test_advisory_lock_live_pg.py in services/common/tests for the full env
contract writeup (HERD_TEST_PG_DSN, HERD_TEST_PG_REQUIRED, issue #1401); this
file follows it identically through herd_common.pg_live_suite.

These tests touch no application schema (no ldap_sync_runs row, no
LdapGroupMapping): _SyncSlot's Postgres branch only ever opens a connection
and takes/releases a session-scoped advisory lock keyed by the fixed
_ADVISORY_LOCK_KEY string, so any reachable Postgres works and nothing here
needs migrations or seeded tables.
"""

from __future__ import annotations

import pytest
from app import database
from app.services import ldap_sync_service
from herd_common.advisory_lock import session_unlock
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy.ext.asyncio import create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def pg_engine(monkeypatch):
    """Point app.database's module-level engine at real Postgres for the
    duration of the test, then restore it. _SyncSlot resolves
    `database.engine` fresh on every call (see module docstring), so this
    monkeypatch is sufficient with no production code changes."""
    engine = create_async_engine(PG_DSN)
    monkeypatch.setattr(database, "engine", engine)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_advisory_lock_key():
    """Belt-and-suspenders: release the fixed _ADVISORY_LOCK_KEY on the
    configured Postgres before and after each test, in case a prior crashed
    run (this file or a stray process) left it held. pg_advisory_unlock on a
    key this session never locked is a documented no-op (returns false),
    never an error, so this is safe to call unconditionally."""
    if _PG.failure is not None:
        yield
        return
    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await session_unlock(conn, ldap_sync_service._ADVISORY_LOCK_KEY)
            await conn.commit()
    except Exception:
        pass
    finally:
        await engine.dispose()
    yield
    try:
        engine = create_async_engine(PG_DSN)
        async with engine.connect() as conn:
            await session_unlock(conn, ldap_sync_service._ADVISORY_LOCK_KEY)
            await conn.commit()
        await engine.dispose()
    except Exception:
        pass


async def test_sync_slot_replica_busy_when_another_connection_holds_the_lock(pg_engine):
    """Simulates another auth replica already running a sync: a raw
    connection (not going through _SyncSlot) takes the SAME advisory lock
    key _SyncSlot uses, then _SyncSlot.__aenter__ on THIS process must
    raise SyncBusyError with reason == "replica" (pinning the exact string,
    per the issue: this is the branch no test has ever exercised)."""
    from herd_common.advisory_lock import session_try_lock

    other_replica_conn = await pg_engine.connect()
    acquired = await session_try_lock(other_replica_conn, ldap_sync_service._ADVISORY_LOCK_KEY)
    assert acquired is True
    await other_replica_conn.commit()

    try:
        with pytest.raises(ldap_sync_service.SyncBusyError) as exc_info:
            async with ldap_sync_service._SyncSlot():
                pytest.fail("should not have acquired the slot while another replica holds it")
        assert exc_info.value.reason == "replica"

        # The failed __aenter__ must not have left the in-process asyncio
        # lock held (it releases everything it acquired before raising),
        # so a second attempt (still contended by the other replica) fails
        # the SAME way rather than a different, in-process "in_process".
        with pytest.raises(ldap_sync_service.SyncBusyError) as exc_info_2:
            async with ldap_sync_service._SyncSlot():
                pytest.fail("should still be busy: the other replica has not released")
        assert exc_info_2.value.reason == "replica"
    finally:
        await session_unlock(other_replica_conn, ldap_sync_service._ADVISORY_LOCK_KEY)
        await other_replica_conn.close()


async def test_sync_slot_acquires_cleanly_once_the_other_replica_releases(pg_engine):
    """After the contending replica releases, _SyncSlot must acquire
    cleanly (no lingering SyncBusyError from a stale lock), and its own
    __aexit__ must release the advisory lock so a THIRD acquire attempt
    also succeeds (proving the lock doesn't leak past the context)."""
    from herd_common.advisory_lock import session_try_lock

    other_replica_conn = await pg_engine.connect()
    acquired = await session_try_lock(other_replica_conn, ldap_sync_service._ADVISORY_LOCK_KEY)
    assert acquired is True
    await other_replica_conn.commit()

    with pytest.raises(ldap_sync_service.SyncBusyError):
        async with ldap_sync_service._SyncSlot():
            pytest.fail("should still be busy before the release")

    await session_unlock(other_replica_conn, ldap_sync_service._ADVISORY_LOCK_KEY)
    await other_replica_conn.close()

    # Released: _SyncSlot now acquires cleanly.
    async with ldap_sync_service._SyncSlot() as slot:
        assert slot.lock_conn is not None

    # __aexit__ released the advisory lock (and closed lock_conn), so a
    # fresh contender can now take it too.
    contender = await pg_engine.connect()
    try:
        acquired = await session_try_lock(contender, ldap_sync_service._ADVISORY_LOCK_KEY)
        assert acquired is True
        await contender.commit()
    finally:
        await session_unlock(contender, ldap_sync_service._ADVISORY_LOCK_KEY)
        await contender.close()
