"""Unit tests for the shared Postgres advisory-lock helpers (issue #513 item 5).

Covers both lock styles (session-scoped try-lock, transaction-scoped
blocking lock), the shared SQLite no-op gate, and a regression pin on
advisory_key_from_string's exact derivation formula: reservations'
_acquire_device_locks depends on this value staying byte-identical to the
inline sha256-truncated-hex formula it replaces, since two replicas
computing different keys for the same device during a rolling deploy would
silently stop serializing against each other.
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from herd_common.advisory_lock import (
    advisory_key_from_string,
    is_postgres_dialect,
    session_try_lock,
    session_unlock,
    xact_lock,
)

# ---------------------------------------------------------------------------
# is_postgres_dialect: the shared SQLite no-op gate.
# ---------------------------------------------------------------------------


def test_is_postgres_dialect_true_for_postgresql():
    assert is_postgres_dialect("postgresql") is True


@pytest.mark.parametrize("name", ["sqlite", "mysql", "", None])
def test_is_postgres_dialect_false_otherwise(name):
    assert is_postgres_dialect(name) is False


# ---------------------------------------------------------------------------
# advisory_key_from_string: byte-identical to the formula it replaces
# (reservations._acquire_device_locks: int(sha256(s).hexdigest()[:15], 16)).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "11111111-1111-1111-1111-111111111111",
        "some-device-id",
        "",
        "unicode-éè",
    ],
)
def test_advisory_key_from_string_matches_reference_formula(value):
    expected = int(hashlib.sha256(value.encode()).hexdigest()[:15], 16)
    assert advisory_key_from_string(value) == expected


def test_advisory_key_from_string_is_deterministic():
    assert advisory_key_from_string("device-1") == advisory_key_from_string("device-1")


def test_advisory_key_from_string_differs_across_inputs():
    assert advisory_key_from_string("device-1") != advisory_key_from_string("device-2")


def test_advisory_key_from_string_fits_signed_bigint():
    # 15 hex digits = 60 bits, comfortably inside signed int64's 63 usable
    # bits; Postgres's pg_advisory_xact_lock(bigint) must never overflow.
    key = advisory_key_from_string("some-arbitrarily-long-device-identifier-string")
    assert 0 <= key < 2**63


def test_advisory_key_from_string_respects_hex_digits_override():
    value = "device-1"
    default = advisory_key_from_string(value)
    shorter = advisory_key_from_string(value, hex_digits=8)
    assert shorter != default
    assert shorter == int(hashlib.sha256(value.encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# xact_lock: transaction-scoped, blocking, self-gated on dialect.
# ---------------------------------------------------------------------------


def _mock_session(dialect_name):
    session = MagicMock()
    session.bind = MagicMock()
    session.bind.dialect.name = dialect_name
    session.execute = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_xact_lock_noop_on_sqlite():
    session = _mock_session("sqlite")
    await xact_lock(session, 12345)
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_xact_lock_noop_when_bind_is_none():
    session = MagicMock()
    session.bind = None
    session.execute = AsyncMock()
    await xact_lock(session, 12345)
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_xact_lock_issues_pg_advisory_xact_lock_on_postgres():
    session = _mock_session("postgresql")
    await xact_lock(session, 987654321)
    session.execute.assert_awaited_once()
    (stmt, params), _kwargs = session.execute.await_args
    assert "pg_advisory_xact_lock" in str(stmt)
    assert params == {"key": 987654321}


@pytest.mark.asyncio
async def test_xact_lock_one_call_per_key_matches_reservations_loop_shape():
    # Mirrors _acquire_device_locks's loop: N devices, N calls, each gated
    # the same way (the dialect check happens on every call rather than
    # once outside the loop, since xact_lock is self-contained).
    session = _mock_session("postgresql")
    for device_str in ("device-a", "device-b", "device-c"):
        await xact_lock(session, advisory_key_from_string(device_str))
    assert session.execute.await_count == 3


# ---------------------------------------------------------------------------
# session_try_lock / session_unlock: session-scoped, non-blocking,
# hashtext()-keyed by a string, self-gated on dialect (issue #513 round-3
# item 5: symmetric with xact_lock's own internal gate).
# ---------------------------------------------------------------------------


def _mock_connection(scalar_result, dialect_name="postgresql"):
    conn = MagicMock()
    conn.dialect.name = dialect_name
    result = MagicMock()
    result.scalar_one.return_value = scalar_result
    conn.execute = AsyncMock(return_value=result)
    return conn


@pytest.mark.asyncio
async def test_session_try_lock_acquired_returns_true():
    conn = _mock_connection(True)
    acquired = await session_try_lock(conn, "herd_ldap_group_sync")
    assert acquired is True
    conn.execute.assert_awaited_once()
    (stmt, params), _kwargs = conn.execute.await_args
    assert "pg_try_advisory_lock" in str(stmt)
    assert "hashtext" in str(stmt)
    assert params == {"key": "herd_ldap_group_sync"}


@pytest.mark.asyncio
async def test_session_try_lock_busy_returns_false():
    conn = _mock_connection(False)
    acquired = await session_try_lock(conn, "herd_ldap_group_sync")
    assert acquired is False


@pytest.mark.asyncio
async def test_session_unlock_issues_pg_advisory_unlock():
    conn = MagicMock()
    conn.dialect.name = "postgresql"
    conn.execute = AsyncMock()
    await session_unlock(conn, "herd_ldap_group_sync")
    conn.execute.assert_awaited_once()
    (stmt, params), _kwargs = conn.execute.await_args
    assert "pg_advisory_unlock" in str(stmt)
    assert "hashtext" in str(stmt)
    assert params == {"key": "herd_ldap_group_sync"}


@pytest.mark.asyncio
async def test_session_try_lock_and_unlock_use_the_same_key_param_shape():
    # Both statements parameterize the SAME key string identically (a bound
    # parameter, not a literal), so hashtext() hashes the identical value on
    # both sides regardless of which connection issues it.
    conn = _mock_connection(True)
    await session_try_lock(conn, "herd_ldap_group_sync")
    await session_unlock(conn, "herd_ldap_group_sync")
    first_params = conn.execute.await_args_list[0].args[1]
    second_params = conn.execute.await_args_list[1].args[1]
    assert first_params == second_params == {"key": "herd_ldap_group_sync"}


# ---------------------------------------------------------------------------
# Round-3 item 5: session_try_lock/session_unlock self-gate on dialect,
# symmetric with xact_lock, so a caller cannot get a raw SQL error on
# SQLite by copying xact_lock's usage pattern.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_try_lock_noop_on_sqlite_returns_true_no_sql():
    conn = _mock_connection(True, dialect_name="sqlite")
    acquired = await session_try_lock(conn, "herd_ldap_group_sync")
    assert acquired is True
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_unlock_noop_on_sqlite_no_sql():
    conn = MagicMock()
    conn.dialect.name = "sqlite"
    conn.execute = AsyncMock()
    await session_unlock(conn, "herd_ldap_group_sync")
    conn.execute.assert_not_awaited()
