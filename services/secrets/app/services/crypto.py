"""Envelope-encryption primitives for the secrets service (issue #39, ADR 0003).

Two layers: secret values are encrypted with a random data-encryption key (DEK)
under AES-GCM, and each DEK is stored only wrapped (AES-GCM-encrypted) by the
key-encryption key (KEK) supplied via SECRETS_KEK. A database dump therefore
contains only ciphertext, nonces, and wrapped DEKs, none of which is usable
without the KEK from the environment.

Associated data binds every ciphertext to its context: a wrapped DEK to its key
version, a value to its (secret_id, key_version) pair. Moving a ciphertext to a
different row or key version fails the GCM auth tag (InvalidTag) instead of
decrypting.

Wrapped DEKs and the wire format of wrap_dek are nonce (12 bytes) || ciphertext.
Value nonces are stored in their own column.
"""

import base64
import binascii
import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
DEK_BYTES = 32
NONCE_BYTES = 12


class KekError(RuntimeError):
    """Key material is missing or malformed; the service must not boot."""


def load_kek(encoded: str, *, var_name: str = "SECRETS_KEK") -> bytes:
    """Decode and validate a base64 KEK. Raises KekError; never returns a default."""
    if not encoded:
        raise KekError(
            f"{var_name} is not set. The secrets service refuses to start without "
            f"key material: set {var_name} to a base64-encoded 32-byte key."
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KekError(f"{var_name} is not valid base64: {exc}") from exc
    if len(raw) != KEK_BYTES:
        raise KekError(f"{var_name} must decode to exactly {KEK_BYTES} bytes, got {len(raw)}.")
    return raw


def generate_dek() -> bytes:
    return os.urandom(DEK_BYTES)


def _wrap_aad(version: int) -> bytes:
    return f"herd-secrets:kek-wrap:{version}".encode()


def _value_aad(secret_id: uuid.UUID, key_version: int) -> bytes:
    return f"herd-secrets:value:{secret_id}:{key_version}".encode()


def wrap_dek(kek: bytes, dek: bytes, version: int) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(kek).encrypt(nonce, dek, _wrap_aad(version))


def unwrap_dek(kek: bytes, wrapped: bytes, version: int) -> bytes:
    """Raises cryptography.exceptions.InvalidTag on a wrong KEK or tampered blob."""
    nonce, ciphertext = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
    return AESGCM(kek).decrypt(nonce, ciphertext, _wrap_aad(version))


def encrypt_value(
    dek: bytes, plaintext: bytes, *, secret_id: uuid.UUID, key_version: int
) -> tuple[bytes, bytes]:
    """Returns (nonce, ciphertext) with the auth tag appended to the ciphertext."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, _value_aad(secret_id, key_version))
    return nonce, ciphertext


def decrypt_value(
    dek: bytes, nonce: bytes, ciphertext: bytes, *, secret_id: uuid.UUID, key_version: int
) -> bytes:
    """Raises cryptography.exceptions.InvalidTag on tampering or a swapped row."""
    return AESGCM(dek).decrypt(nonce, ciphertext, _value_aad(secret_id, key_version))
