"""Local-account password hashing: bcrypt called directly (issue #1355).

This replaced passlib 1.7.4's `CryptContext(schemes=["bcrypt"], deprecated="auto")`.
The compatibility contract: `verify_password` accepts exactly the (password, stored
hash) pairs passlib accepted, and every other pair returns False; it never raises.
`tests/data/passlib_1_7_4_fixtures.json` records 802 decisions passlib made, and
`tests/test_password_hashing.py` checks every one. What passlib did, and so what this
module does:

- The password is encoded as UTF-8 and only its first 72 bytes (`BCRYPT_MAX_BYTES`)
  reach bcrypt. passlib dropped the rest silently; bcrypt 5 raises on longer input,
  so the cut is explicit here. Two passwords with the same first 72 bytes are the
  same password, even when the cut falls inside a multi-byte character.
- A password holding a NUL character, or longer than 4096 UTF-8 bytes
  (`MAX_PASSWORD_BYTES`), or not encodable as UTF-8, is refused: `hash_password`
  raises ValueError and `verify_password` returns False. `validate_password` is
  that rule alone, the ONE definition of a legal password (issue #1426):
  `RegisterRequest` runs it as a field validator, so registration refuses exactly
  what hashing refuses, as a 422. Its messages are fixed and never hold the
  password.
- A stored hash is usable when it reads `$<ident>$<cost>$<22-char salt><31-char
  checksum>` with ident `2`, `2a`, `2b`, or `2y` and cost 04 to `MAX_VERIFY_COST`
  (14). `$2x$` and every other shape are refused. A `$2$` hash is checked as
  passlib emulated it, with the password repeated to 72 bytes. This module checks
  that under `$2a$`; passlib on bcrypt 4.0.1 used its fallback ident `$2b$`. The
  result is the same, because `$2a$` and `$2b$` differ only for inputs over 72
  bytes, and the secret here is never longer than 72.
- The unused low bits of the last salt character (4 bits) and the last checksum
  character (2 bits) are cleared before checking, as passlib did (it warned).
- New hashes are `$2b$` at cost 12 (`BCRYPT_IDENT`, `BCRYPT_ROUNDS`), passlib's
  defaults. A stored hash at any other usable cost still verifies at its own cost;
  nothing rehashes on login (passlib's `needs_update` was never called).

One deliberate departure from passlib (issue #1425): passlib checked a well-formed
hash at any cost up to 31, and each cost step doubles the work, so a cost-31 row
held the single-worker event loop for about 31 hours per login attempt. A hash
above `MAX_VERIFY_COST` is unusable here, refused like any other unusable hash
without bcrypt ever running on it. HERD writes only cost 12, and no recorded
passlib decision (`tests/data/passlib_1_7_4_fixtures.json`) is above the ceiling,
so no stored hash HERD or passlib wrote changes its answer.

Every `verify_password` call runs exactly one bcrypt computation: against the stored
hash, at that hash's own cost, when it is usable and the password is acceptable, else
against `DUMMY_HASH`, at cost 12, the ident and cost new hashes get. Every hash HERD
wrote is cost 12, so a refusal costs what a check of a real account costs
(IAM-LOGIN-2). Both functions are synchronous and CPU-bound.

Off the event loop (IAM-HASH-7). A cost-12 check holds a core for about a fifth
of a second, and auth runs one process with one event loop, so a request path
never calls the two functions above directly: it enters `hashing_slot()` and runs
them through `run_hashing`, which uses a worker thread (bcrypt releases the GIL
while it computes). Two limiters, both of size `hash_threads()`, keep the work
bounded:

- the slot limiter admits at most that many requests to the hashing section at a
  time. A request waits for a slot at most `HASH_WAIT_SECONDS`, and at most
  `HASH_MAX_WAITING_PER_THREAD` requests per thread wait at once; beyond either
  bound `hashing_slot()` raises `HashingBusyError`, which the routes answer 503
  with `Retry-After: HASH_RETRY_AFTER_SECONDS`. A request waiting for a slot holds
  no database connection: the routes enter the slot before their first query.
- the worker limiter is what `run_hashing` hands `anyio.to_thread.run_sync`, so
  bcrypt never takes a thread from anyio's shared default pool (40 threads, used
  by every LDAP call), and never runs on more than `hash_threads()` threads at
  once, slot or no slot (the startup seed calls `run_hashing` without one).

The work per answer is unchanged: still exactly one bcrypt per login answer.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import anyio
import bcrypt

logger = logging.getLogger(__name__)

BCRYPT_ROUNDS = 12
BCRYPT_IDENT = b"2b"
# bcrypt reads at most 72 bytes of a password.
BCRYPT_MAX_BYTES = 72
# passlib's MAX_PASSWORD_SIZE: a longer password was refused, never truncated.
MAX_PASSWORD_BYTES = 4096
# The highest stored-hash cost verify_password will compute (issue #1425). Two
# steps above BCRYPT_ROUNDS: room to raise the cost of new hashes twice, and one
# check stays under about a second on the dev host (cost 14 measured 0.87 s).
# It must never fall below BCRYPT_ROUNDS, or every new hash would be unusable.
MAX_VERIFY_COST = 14

_BCRYPT64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_STORED_HASH = re.compile(
    r"\$(?P<ident>2|2a|2b|2y)\$(?P<cost>0[4-9]|[12][0-9]|3[01])\$"
    r"(?P<salt>[./A-Za-z0-9]{22})(?P<checksum>[./A-Za-z0-9]{31})"
)
UNUSABLE_HASH_MESSAGE = "Stored password hash is not a usable bcrypt hash"

# validate_password's refusals. Fixed text: a message never holds the password, so
# it is safe in a 422 body, a log line, or a startup traceback.
PASSWORD_NOT_A_STRING_MESSAGE = "Password must be a string"
PASSWORD_NOT_UTF8_MESSAGE = "Password must be encodable as UTF-8"
PASSWORD_TOO_LONG_MESSAGE = f"Password must be at most {MAX_PASSWORD_BYTES} bytes as UTF-8"
PASSWORD_NUL_MESSAGE = "Password must not contain a NUL character"


def _encoded_password(password: object) -> bytes:
    """The whole UTF-8 form of a legal password; ValueError for a refused one."""
    if not isinstance(password, str):
        raise ValueError(PASSWORD_NOT_A_STRING_MESSAGE)
    try:
        raw = password.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate
        # from None: UnicodeEncodeError's own text quotes the offending character.
        raise ValueError(PASSWORD_NOT_UTF8_MESSAGE) from None
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(PASSWORD_TOO_LONG_MESSAGE)
    if b"\x00" in raw:
        raise ValueError(PASSWORD_NUL_MESSAGE)
    return raw


def validate_password(password: str) -> str:
    """`password` unchanged if `hash_password` would take it, else ValueError.

    The one definition of a legal password at every entry point that sets one
    (issue #1426). It decides exactly what hashing decides, never more: the
    length bounds of a request schema are separate rules (IAM-REG-2).
    """
    _encoded_password(password)
    return password


def _password_bytes(password: str) -> bytes:
    """The bytes bcrypt reads for `password`; ValueError for a refused password."""
    return _encoded_password(password)[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """A new `$2b$` hash at cost `BCRYPT_ROUNDS` of the password's first 72 bytes."""
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS, prefix=BCRYPT_IDENT)
    return bcrypt.hashpw(_password_bytes(password), salt).decode("ascii")


DUMMY_HASH = hash_password("not-a-real-password")
_DUMMY_HASH_BYTES = DUMMY_HASH.encode("ascii")


def _clear_low_bits(char: str, bits: int) -> str:
    return _BCRYPT64[_BCRYPT64.index(char) >> bits << bits]


def _usable_hash(stored_hash: object) -> tuple[bytes, bool] | None:
    """The hash to hand bcrypt and whether it is a `$2$` hash, or None if unusable."""
    if not isinstance(stored_hash, str):
        return None
    match = _STORED_HASH.fullmatch(stored_hash)
    if match is None or int(match["cost"]) > MAX_VERIFY_COST:
        return None
    salt = match["salt"][:-1] + _clear_low_bits(match["salt"][-1], 4)
    checksum = match["checksum"][:-1] + _clear_low_bits(match["checksum"][-1], 2)
    legacy_2 = match["ident"] == "2"
    ident = "2a" if legacy_2 else match["ident"]
    return f"${ident}${match['cost']}${salt}{checksum}".encode("ascii"), legacy_2


def _refuse_after_dummy_check() -> bool:
    bcrypt.checkpw(b"", _DUMMY_HASH_BYTES)
    return False


def verify_password(password: str, stored_hash: str | None) -> bool:
    """True exactly when passlib would have accepted `password` for `stored_hash`."""
    usable = _usable_hash(stored_hash)
    try:
        secret = _password_bytes(password)
    except ValueError:
        secret = None
    if usable is None:
        logger.warning(UNUSABLE_HASH_MESSAGE, extra={"action": "password_hash_unusable"})
        return _refuse_after_dummy_check()
    if secret is None:
        return _refuse_after_dummy_check()
    config, legacy_2 = usable
    if legacy_2 and secret:
        secret = (secret * (BCRYPT_MAX_BYTES // len(secret) + 1))[:BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(secret, config)
    except ValueError as exc:
        # The pattern admits only hashes bcrypt reads; this is a backstop.
        logger.warning(
            UNUSABLE_HASH_MESSAGE,
            extra={"action": "password_hash_unusable", "error": type(exc).__name__},
        )
        return _refuse_after_dummy_check()


# -- Off the event loop (IAM-HASH-7) ---------------------------------------------

# How long a request waits for a hashing slot before it is answered 503, and the
# Retry-After that answer carries. Fixed in code: the thread count is the knob.
HASH_WAIT_SECONDS = 5.0
HASH_RETRY_AFTER_SECONDS = 5
# Requests that may wait for a slot at once, per hashing thread. A cost-12 check
# takes about 0.2 s, so one thread clears about 25 in HASH_WAIT_SECONDS; a request
# further back would time out anyway, so it is refused at once instead of being
# held in memory for five seconds.
HASH_MAX_WAITING_PER_THREAD = 25
# The automatic thread count never exceeds this: auth shares its host with eleven
# other services and Postgres.
MAX_AUTO_HASH_THREADS = 4

_T = TypeVar("_T")


class HashingBusyError(Exception):
    """No hashing slot came free within the wait bound (IAM-HASH-7)."""


def available_cpus() -> int:
    """The CPUs this process may run on (its affinity mask, not the host total)."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - not Linux
        return os.cpu_count() or 1


def auto_hash_threads(cpus: int) -> int:
    """The thread count when AUTH_PASSWORD_HASH_THREADS is 0: half the CPUs, at
    least 1 and at most MAX_AUTO_HASH_THREADS."""
    return max(1, min(MAX_AUTO_HASH_THREADS, cpus // 2))


def effective_hash_threads(configured: int, cpus: int) -> int:
    """`configured` when it is positive, else the automatic count."""
    return configured if configured > 0 else auto_hash_threads(cpus)


class HashingPool:
    """The slot and worker limiters, sized once (see the module docstring)."""

    def __init__(
        self,
        threads: int,
        *,
        wait_seconds: float = HASH_WAIT_SECONDS,
        max_waiting: int | None = None,
    ) -> None:
        if threads < 1:
            raise ValueError("a hashing pool needs at least one thread")
        self.threads = threads
        self.wait_seconds = wait_seconds
        self.max_waiting = (
            threads * HASH_MAX_WAITING_PER_THREAD if max_waiting is None else max_waiting
        )
        self._slots = anyio.CapacityLimiter(threads)
        self._workers = anyio.CapacityLimiter(threads)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._slots.statistics().tasks_waiting >= self.max_waiting:
            raise HashingBusyError
        borrower = object()
        acquired = False
        with anyio.move_on_after(self.wait_seconds):
            await self._slots.acquire_on_behalf_of(borrower)
            acquired = True
        if not acquired:
            raise HashingBusyError
        try:
            yield
        finally:
            self._slots.release_on_behalf_of(borrower)

    async def run(self, func: Callable[..., _T], *args: object) -> _T:
        return await anyio.to_thread.run_sync(func, *args, limiter=self._workers)

    def busy_slots(self) -> int:
        return int(self._slots.borrowed_tokens)

    def busy_threads(self) -> int:
        return int(self._workers.borrowed_tokens)


_pool: HashingPool | None = None


def hashing_pool() -> HashingPool:
    """The process's pool, built on first use from AUTH_PASSWORD_HASH_THREADS."""
    global _pool
    if _pool is None:
        from app.config import settings

        _pool = HashingPool(
            effective_hash_threads(settings.auth_password_hash_threads, available_cpus())
        )
        logger.info(
            "Password hashing runs on %d worker threads",
            _pool.threads,
            extra={"action": "password_hash_threads", "threads": _pool.threads},
        )
    return _pool


def set_hashing_pool(pool: HashingPool | None) -> None:
    """Replace the process's pool (tests); None rebuilds it from settings on next use."""
    global _pool
    _pool = pool


def hashing_slot():
    """`async with hashing_slot():` around a request's hashing section; raises
    HashingBusyError when no slot comes free in time (IAM-HASH-7)."""
    return hashing_pool().slot()


async def run_hashing(func: Callable[..., _T], *args: object) -> _T:
    """`func(*args)` on one of the pool's worker threads, never on the event loop."""
    return await hashing_pool().run(func, *args)
