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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KeyVersion, Secret
from app.services import crypto
from app.services.crypto import KekError

logger = logging.getLogger(__name__)


class Keyring:
    def __init__(self, kek: bytes, deks: dict[int, bytes], active_version: int):
        self._kek = kek
        self._deks = deks
        self.active_version = active_version

    def dek(self, version: int) -> bytes:
        return self._deks[version]

    @property
    def active_dek(self) -> bytes:
        return self._deks[self.active_version]


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
    """
    rows = (await session.execute(select(KeyVersion).order_by(KeyVersion.version))).scalars().all()
    new_version = max(r.version for r in rows) + 1
    new_dek = crypto.generate_dek()
    wrapped = crypto.wrap_dek(keyring._kek, new_dek, new_version)
    session.add(KeyVersion(version=new_version, wrapped_dek=wrapped))

    secret_rows = (await session.execute(select(Secret))).scalars().all()
    for secret in secret_rows:
        plaintext = crypto.decrypt_value(
            keyring.dek(secret.key_version),
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
