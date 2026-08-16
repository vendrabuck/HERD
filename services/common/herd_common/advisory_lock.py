"""Shared Postgres advisory-lock helpers (issue #513 item 5).

Two independent conventions for scoping a Postgres advisory lock exist
across services; this module single-sources both plus their shared
SQLite-test no-op gate before a third service invents a third variant.

- Session-scoped, non-blocking try-acquire, key derived from a STRING via
  Postgres's own hashtext() (auth's ldap_sync_service._SyncSlot, the ADR
  0011 S1 run invariant coordinating sync-now across replicas):
  session_try_lock / session_unlock. The lock is held across many
  operations on ONE dedicated connection (advisory locks are
  session-scoped, not transaction-scoped, and do not release on commit or
  rollback), so the caller owns that connection's lifetime and must
  acquire and release on the SAME connection.
- Transaction-scoped, blocking acquire, key derived from a Python-computed
  int via SHA256 (reservations._acquire_device_locks, serializing
  concurrent creates on the same device set): xact_lock, keyed through
  advisory_key_from_string. Auto-releases on the session's next commit or
  rollback.

is_postgres_dialect is the shared no-op gate (SQLite unit tests run with no
real DB and no advisory-lock support). Both styles self-gate: xact_lock and
session_try_lock/session_unlock all check it internally against whatever
they were handed (a session or an already-open connection) and no-op (or,
for session_try_lock, return True: nothing to coordinate) rather than
issue SQL a non-Postgres dialect would reject. It is also exposed standalone
for the session-scoped style's ADDITIONAL, cheaper pre-connection check
(auth's _SyncSlot checks it against the ENGINE before ever opening a
dedicated connection at all, since opening the connection is itself the
expensive step that style is built around avoiding); the self-gate inside
session_try_lock/session_unlock is a safety net for a connection some
OTHER caller already opened, not a replacement for that earlier check.

A rolling-deploy-sensitive fixed key string (like auth's) must stay
byte-identical across every build using it: hashtext() hashes the STRING
VALUE server-side, so the SQL syntax carrying it (a literal vs. a bound
parameter) does not change the resulting lock key, but the string constant
itself must never change without accepting a rolling-deploy window where
old and new replicas hold different locks for what is meant to be one run
invariant. That constant is owned by its call site (ldap_sync_service), not
this module.
"""

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


def is_postgres_dialect(dialect_name: str | None) -> bool:
    """True when dialect_name is "postgresql". Both advisory-lock styles in
    this module are no-ops on every other dialect (SQLite unit tests)."""
    return dialect_name == "postgresql"


def advisory_key_from_string(value: str, *, hex_digits: int = 15) -> int:
    """Derive a Postgres bigint advisory-lock key from an arbitrary string
    via SHA256, truncated to hex_digits hex characters (default 15, 60
    bits, comfortably inside a signed 64-bit bigint). This is the xact_lock
    convention: many distinct per-item keys computed client-side, unlike
    session_try_lock's single server-hashed string key. Byte-identical to
    the formula it replaces (reservations._acquire_device_locks)."""
    return int(hashlib.sha256(value.encode()).hexdigest()[:hex_digits], 16)


async def xact_lock(session: AsyncSession, key: int) -> None:
    """Acquire a transaction-scoped, blocking Postgres advisory lock on key.

    Auto-releases on the session's next commit or rollback. No-op on any
    non-Postgres dialect (session.bind is the bound Engine; SQLite unit
    tests carry no advisory-lock support)."""
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    if not is_postgres_dialect(dialect_name):
        return
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def session_try_lock(conn: AsyncConnection, key: str) -> bool:
    """Attempt a session-scoped, non-blocking Postgres advisory lock keyed by
    the string key (hashed server-side via hashtext()). Returns whether the
    lock was acquired; False means another session already holds it.

    pg_try_advisory_lock autobegins a transaction on this connection if none
    is already open; session-level advisory locks survive both commit and
    rollback (only session end releases them), so a caller that intends to
    hold the lock for a while should commit promptly rather than leave this
    connection idling in an open transaction. The caller owns the
    connection's lifetime and must release via session_unlock on the SAME
    connection (advisory locks are session-scoped, not
    connection-pool-scoped).

    Self-gated (issue #513 round-3 item 5, symmetric with xact_lock): a
    non-Postgres dialect returns True (lock "acquired", nothing to
    coordinate, the no-op-means-proceed shape xact_lock's no-op already
    has) without issuing any SQL, so a caller cannot get a raw SQL error on
    SQLite by copying xact_lock's usage pattern. This is a safety net for
    a connection that is already open; a caller that wants to avoid even
    OPENING a connection on a non-Postgres engine (auth's _SyncSlot) should
    still check is_postgres_dialect against the ENGINE first, since
    opening the connection is itself the expensive step this style is
    built around avoiding.
    """
    if not is_postgres_dialect(conn.dialect.name):
        return True
    result = await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key})
    return bool(result.scalar_one())


async def session_unlock(conn: AsyncConnection, key: str) -> None:
    """Release a session-scoped advisory lock acquired via session_try_lock
    on the SAME connection. Self-gated like session_try_lock: a no-op on
    any non-Postgres dialect."""
    if not is_postgres_dialect(conn.dialect.name):
        return
    await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key})
