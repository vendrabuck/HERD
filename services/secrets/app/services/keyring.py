"""The in-memory key ring: KEK bootstrap, re-wrap, and DEK rotation (issue #39).

Plaintext DEKs exist only in this process's memory; the database holds them
wrapped (see app/services/crypto.py). Bootstrap runs at service startup and
raises KekError on missing, malformed, or mismatched key material, which is the
refuse-to-boot behavior of ADR 0003 decision point 3.
"""

import json
import logging
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from herd_common.advisory_lock import advisory_key_from_string, xact_lock
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KeyVersion, Secret
from app.services import crypto
from app.services.crypto import KekError

logger = logging.getLogger(__name__)

# Issue #1085: every DEK rotation, in every replica, takes this one
# transaction-scoped Postgres advisory lock before it reads the key table, so
# a second rotation waits for the first to commit and then picks the next
# version number instead of colliding on the key_versions primary key. The
# string is fixed: a rolling deploy must derive the same key on both images.
DEK_ROTATION_LOCK_KEY = advisory_key_from_string("herd-secrets-dek-rotation")


# Fixed 503 detail for a key version this process cannot load: no version
# number, no KEK detail (the log carries the version).
KEY_VERSION_UNAVAILABLE_DETAIL = "Secret key material is unavailable to this service"
ROTATION_CONFLICT_DETAIL = "Another key rotation committed first; nothing was changed. Retry."


def key_unavailable_http(exc: "KeyVersionUnavailableError") -> HTTPException:
    """The 503 a route answers for KeyVersionUnavailableError, logged once."""
    logger.error(
        "key version %s is not available to this process (missing row or a KEK mismatch)",
        exc.version,
        extra={"action": "key_version_unavailable", "key_version": exc.version},
    )
    return HTTPException(status_code=503, detail=KEY_VERSION_UNAVAILABLE_DETAIL)


class KeyVersionUnavailableError(Exception):
    """A key version is missing from the key table, or does not unwrap under
    this process's KEK. The message carries only the version number."""

    def __init__(self, version: int):
        super().__init__(f"key version {version} is not available")
        self.version = version


class RotationConflictError(Exception):
    """Another rotation committed the same new key version first (the
    backstop for a database without advisory locks)."""


class Keyring:
    """This process's unwrapped DEKs, by version.

    The key table is the source of truth; this is a cache of it (issue
    #1085). A version another replica created is read and unwrapped on first
    use (`load_dek`), and the version to encrypt new data under is read from
    the table on every write (`current`), so a rotation in one replica binds
    every replica at once, with no restart.
    """

    def __init__(self, kek: bytes, deks: dict[int, bytes], active_version: int):
        self._kek = kek
        self._deks = deks
        self.active_version = active_version

    def dek(self, version: int) -> bytes:
        return self._deks[version]

    @property
    def active_dek(self) -> bytes:
        return self._deks[self.active_version]

    async def load_dek(self, session: AsyncSession, version: int) -> bytes:
        """The DEK for `version`, read from the key table when this process
        has not seen it (a rotation in another replica). Raises
        KeyVersionUnavailableError when the row is missing or does not unwrap
        under this process's KEK."""
        dek = self._deks.get(version)
        if dek is not None:
            return dek
        row = await session.get(KeyVersion, version)
        if row is None:
            raise KeyVersionUnavailableError(version)
        try:
            dek = crypto.unwrap_dek(self._kek, row.wrapped_dek, version)
        except InvalidTag:
            raise KeyVersionUnavailableError(version) from None
        self._deks[version] = dek
        logger.info(
            "keyring_loaded_version",
            extra={"action": "load_key_version", "key_version": version},
        )
        return dek

    async def current(self, session: AsyncSession) -> tuple[int, bytes]:
        """The version and DEK new ciphertext is written under: the highest
        version the key table holds unretired, not this process's memory, so
        a replica that missed a rotation does not keep encrypting under the
        retired key."""
        version = await session.scalar(
            select(func.max(KeyVersion.version)).where(KeyVersion.retired_at.is_(None))
        )
        if version is None:
            # Every row retired cannot happen through rotate_dek; keep the
            # in-memory answer rather than failing the write.
            version = self.active_version
        dek = await self.load_dek(session, version)
        self.active_version = version
        return version, dek


async def bootstrap_keyring(
    session: AsyncSession, *, kek_encoded: str, previous_kek_encoded: str = ""
) -> Keyring:
    """Load and unwrap every key version; create version 1 on first boot.

    A wrapped DEK that fails under the current KEK is retried with
    SECRETS_KEK_PREVIOUS (if set) and re-wrapped under the current KEK, which is
    the online KEK-rotation path: O(number of key versions), no secret row is
    touched. Failing both KEKs is a hard KekError: serving with undecryptable
    secrets would turn every reveal into a 500 at request time.
    """
    kek = crypto.load_kek(kek_encoded)
    previous = (
        crypto.load_kek(previous_kek_encoded, var_name="SECRETS_KEK_PREVIOUS")
        if previous_kek_encoded
        else None
    )

    rows = (await session.execute(select(KeyVersion).order_by(KeyVersion.version))).scalars().all()

    if not rows:
        dek = crypto.generate_dek()
        session.add(KeyVersion(version=1, wrapped_dek=crypto.wrap_dek(kek, dek, 1)))
        await session.commit()
        logger.info("keyring_initialized", extra={"action": "create_key_version"})
        return Keyring(kek, {1: dek}, active_version=1)

    deks: dict[int, bytes] = {}
    rewrapped = 0
    for row in rows:
        try:
            deks[row.version] = crypto.unwrap_dek(kek, row.wrapped_dek, row.version)
        except InvalidTag:
            if previous is None:
                raise KekError(
                    f"key version {row.version} does not unwrap with SECRETS_KEK and no "
                    f"SECRETS_KEK_PREVIOUS is set; refusing to start with undecryptable secrets."
                ) from None
            try:
                dek = crypto.unwrap_dek(previous, row.wrapped_dek, row.version)
            except InvalidTag:
                raise KekError(
                    f"key version {row.version} unwraps with neither SECRETS_KEK nor "
                    f"SECRETS_KEK_PREVIOUS; refusing to start with undecryptable secrets."
                ) from None
            row.wrapped_dek = crypto.wrap_dek(kek, dek, row.version)
            deks[row.version] = dek
            rewrapped += 1
    if rewrapped:
        await session.commit()
        logger.info("keyring_rewrapped", extra={"action": "kek_rotation", "count": rewrapped})

    active = max(r.version for r in rows if r.retired_at is None)
    return Keyring(kek, deks, active_version=active)


async def rotate_dek(session: AsyncSession, keyring: Keyring) -> dict:
    """Introduce a new DEK version and re-encrypt every secret to it.

    O(number of secrets), all in one transaction: on any failure the commit
    never happens and every row still decrypts under its old version. Old key
    versions are retired but retained, so ciphertext written mid-flight by a
    concurrent process (multi-replica deployments) remains decryptable.

    Issue #1085: the rotation lock is taken FIRST, before the key table is
    read, so two rotations (one process or two replicas) run one after the
    other and the second sees the first's version. A secret under a version
    this process never loaded (another replica's rotation) is decrypted
    through `load_dek`. If the new version is taken anyway (a database
    without advisory locks), the transaction is rolled back and
    RotationConflictError is raised; nothing changed.
    """
    try:
        return await _rotate_dek_locked(session, keyring)
    except IntegrityError:
        # The flush of the new key_versions row can happen at any autoflush
        # point (the secrets query, a key lookup) or at commit.
        await session.rollback()
        raise RotationConflictError() from None


async def _rotate_dek_locked(session: AsyncSession, keyring: Keyring) -> dict:
    await xact_lock(session, DEK_ROTATION_LOCK_KEY)
    rows = (await session.execute(select(KeyVersion).order_by(KeyVersion.version))).scalars().all()
    new_version = max(r.version for r in rows) + 1
    new_dek = crypto.generate_dek()
    wrapped = crypto.wrap_dek(keyring._kek, new_dek, new_version)
    session.add(KeyVersion(version=new_version, wrapped_dek=wrapped))

    secret_rows = (await session.execute(select(Secret))).scalars().all()
    for secret in secret_rows:
        plaintext = crypto.decrypt_value(
            await keyring.load_dek(session, secret.key_version),
            secret.nonce,
            secret.ciphertext,
            secret_id=secret.id,
            key_version=secret.key_version,
        )
        secret.nonce, secret.ciphertext = crypto.encrypt_value(
            new_dek, plaintext, secret_id=secret.id, key_version=new_version
        )
        secret.key_version = new_version

    now = datetime.now(timezone.utc)
    for row in rows:
        if row.retired_at is None:
            row.retired_at = now

    await session.commit()

    keyring._deks[new_version] = new_dek
    keyring.active_version = new_version
    logger.info(
        "dek_rotated",
        extra={"action": "dek_rotation", "count": len(secret_rows)},
    )
    return {"new_version": new_version, "reencrypted": len(secret_rows)}


def serialize_data(data: dict) -> bytes:
    """Canonical plaintext form of a secret's data dict."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def deserialize_data(plaintext: bytes) -> dict:
    return json.loads(plaintext.decode())
