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
  raises ValueError and `verify_password` returns False.
- A stored hash is usable when it reads `$<ident>$<cost>$<22-char salt><31-char
  checksum>` with ident `2`, `2a`, `2b`, or `2y` and cost 04 to 31. `$2x$` and every
  other shape are refused. A `$2$` hash is checked as passlib emulated it, with
  the password repeated to 72 bytes. This module checks that under `$2a$`;
  passlib on bcrypt 4.0.1 used its fallback ident `$2b$`. The result is the same,
  because `$2a$` and `$2b$` differ only for inputs over 72 bytes, and the secret
  here is never longer than 72.
- The unused low bits of the last salt character (4 bits) and the last checksum
  character (2 bits) are cleared before checking, as passlib did (it warned).
- New hashes are `$2b$` at cost 12 (`BCRYPT_IDENT`, `BCRYPT_ROUNDS`), passlib's
  defaults. A stored hash at any other usable cost still verifies at its own cost;
  nothing rehashes on login (passlib's `needs_update` was never called).

Every `verify_password` call runs exactly one bcrypt computation: against the stored
hash, at that hash's own cost, when it is usable and the password is acceptable, else
against `DUMMY_HASH`, at cost 12, the ident and cost new hashes get. Every hash HERD
wrote is cost 12, so a refusal costs what a check of a real account costs
(IAM-LOGIN-2). Both functions are synchronous and CPU-bound; callers run them
on the event loop today, as they did with passlib.
"""

from __future__ import annotations

import logging
import re

import bcrypt

logger = logging.getLogger(__name__)

BCRYPT_ROUNDS = 12
BCRYPT_IDENT = b"2b"
# bcrypt reads at most 72 bytes of a password.
BCRYPT_MAX_BYTES = 72
# passlib's MAX_PASSWORD_SIZE: a longer password was refused, never truncated.
MAX_PASSWORD_BYTES = 4096

_BCRYPT64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_STORED_HASH = re.compile(
    r"\$(?P<ident>2|2a|2b|2y)\$(?P<cost>0[4-9]|[12][0-9]|3[01])\$"
    r"(?P<salt>[./A-Za-z0-9]{22})(?P<checksum>[./A-Za-z0-9]{31})"
)
UNUSABLE_HASH_MESSAGE = "Stored password hash is not a usable bcrypt hash"


def _password_bytes(password: str) -> bytes:
    """The bytes bcrypt reads for `password`; ValueError for a refused password."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    raw = password.encode("utf-8")  # UnicodeEncodeError, a ValueError, for a lone surrogate
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password is longer than {MAX_PASSWORD_BYTES} bytes")
    if b"\x00" in raw:
        raise ValueError("password contains a NUL character")
    return raw[:BCRYPT_MAX_BYTES]


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
    if match is None:
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
