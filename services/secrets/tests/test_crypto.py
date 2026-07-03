"""Unit tests for the envelope-encryption primitives (issue #39, ADR 0003).

These pin the two crypto invariants: (1) values and DEKs round-trip only under
the exact key and associated data they were written with, and (2) any tamper,
row swap, or version swap fails the GCM auth tag (InvalidTag) rather than
decrypting.
"""

import base64
import uuid

import pytest
from app.services import crypto
from cryptography.exceptions import InvalidTag

KEK = b"k" * 32
OTHER_KEK = b"K" * 32


def test_load_kek_round_trip():
    encoded = base64.b64encode(KEK).decode()
    assert crypto.load_kek(encoded) == KEK


def test_load_kek_empty_is_a_boot_error():
    with pytest.raises(crypto.KekError, match="SECRETS_KEK is not set"):
        crypto.load_kek("")


def test_load_kek_invalid_base64():
    with pytest.raises(crypto.KekError, match="not valid base64"):
        crypto.load_kek("not-base64!!!")


def test_load_kek_wrong_length():
    encoded = base64.b64encode(b"short").decode()
    with pytest.raises(crypto.KekError, match="exactly 32 bytes"):
        crypto.load_kek(encoded)


def test_load_kek_names_the_variable():
    with pytest.raises(crypto.KekError, match="SECRETS_KEK_PREVIOUS"):
        crypto.load_kek("", var_name="SECRETS_KEK_PREVIOUS")


def test_value_round_trip():
    dek = crypto.generate_dek()
    secret_id = uuid.uuid4()
    nonce, ciphertext = crypto.encrypt_value(dek, b"hunter2", secret_id=secret_id, key_version=1)
    assert (
        crypto.decrypt_value(dek, nonce, ciphertext, secret_id=secret_id, key_version=1)
        == b"hunter2"
    )
    assert b"hunter2" not in ciphertext


def test_tampered_ciphertext_fails_auth_tag():
    dek = crypto.generate_dek()
    secret_id = uuid.uuid4()
    nonce, ciphertext = crypto.encrypt_value(dek, b"hunter2", secret_id=secret_id, key_version=1)
    tampered = bytes([ciphertext[0] ^ 0x01]) + ciphertext[1:]
    with pytest.raises(InvalidTag):
        crypto.decrypt_value(dek, nonce, tampered, secret_id=secret_id, key_version=1)


def test_row_swap_fails_via_aad():
    """A ciphertext moved to a different secret's row must not decrypt."""
    dek = crypto.generate_dek()
    victim, attacker = uuid.uuid4(), uuid.uuid4()
    nonce, ciphertext = crypto.encrypt_value(dek, b"hunter2", secret_id=victim, key_version=1)
    with pytest.raises(InvalidTag):
        crypto.decrypt_value(dek, nonce, ciphertext, secret_id=attacker, key_version=1)


def test_key_version_swap_fails_via_aad():
    dek = crypto.generate_dek()
    secret_id = uuid.uuid4()
    nonce, ciphertext = crypto.encrypt_value(dek, b"hunter2", secret_id=secret_id, key_version=1)
    with pytest.raises(InvalidTag):
        crypto.decrypt_value(dek, nonce, ciphertext, secret_id=secret_id, key_version=2)


def test_dek_wrap_round_trip():
    dek = crypto.generate_dek()
    wrapped = crypto.wrap_dek(KEK, dek, version=1)
    assert crypto.unwrap_dek(KEK, wrapped, version=1) == dek
    assert dek not in wrapped


def test_dek_unwrap_wrong_kek_fails():
    wrapped = crypto.wrap_dek(KEK, crypto.generate_dek(), version=1)
    with pytest.raises(InvalidTag):
        crypto.unwrap_dek(OTHER_KEK, wrapped, version=1)


def test_dek_unwrap_wrong_version_fails_via_aad():
    """A wrapped DEK moved to a different key_versions row must not unwrap."""
    wrapped = crypto.wrap_dek(KEK, crypto.generate_dek(), version=1)
    with pytest.raises(InvalidTag):
        crypto.unwrap_dek(KEK, wrapped, version=2)
