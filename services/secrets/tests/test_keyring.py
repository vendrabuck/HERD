"""Unit tests for keyring bootstrap, KEK rotation, and DEK rotation (issue #39).

In-memory SQLite via StaticPool so every session sees the same database. These
pin the refuse-to-boot contract (ADR 0003 decision point 3) and both rotation
paths: KEK re-wrap at boot (O(key versions)) and DEK rotation (O(secrets),
old versions retained and decryptable).
"""

import base64
import uuid

import pytest
from app.database import Base
from app.models import KeyVersion, Secret
from app.services import crypto
from app.services.crypto import KekError
from app.services.keyring import bootstrap_keyring, rotate_dek, serialize_data
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

KEK_A = base64.b64encode(b"a" * 32).decode()
KEK_B = base64.b64encode(b"b" * 32).decode()


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _add_secret(session, keyring, name: str, data: dict) -> Secret:
    secret = Secret(id=uuid.uuid4(), name=name, type="generic")
    secret.key_version = keyring.active_version
    secret.nonce, secret.ciphertext = crypto.encrypt_value(
        keyring.active_dek,
        serialize_data(data),
        secret_id=secret.id,
        key_version=secret.key_version,
    )
    session.add(secret)
    await session.commit()
    return secret


async def test_first_boot_creates_version_1(session_factory):
    async with session_factory() as session:
        keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
        assert keyring.active_version == 1
        rows = (await session.execute(select(KeyVersion))).scalars().all()
        assert [r.version for r in rows] == [1]


async def test_reboot_recovers_the_same_dek(session_factory):
    async with session_factory() as session:
        first = await bootstrap_keyring(session, kek_encoded=KEK_A)
    async with session_factory() as session:
        second = await bootstrap_keyring(session, kek_encoded=KEK_A)
    assert second.dek(1) == first.dek(1)


async def test_missing_kek_refuses_to_boot(session_factory):
    async with session_factory() as session:
        with pytest.raises(KekError, match="refuses to start"):
            await bootstrap_keyring(session, kek_encoded="")


async def test_wrong_kek_refuses_to_boot(session_factory):
    async with session_factory() as session:
        await bootstrap_keyring(session, kek_encoded=KEK_A)
    async with session_factory() as session:
        with pytest.raises(KekError, match="refusing to start"):
            await bootstrap_keyring(session, kek_encoded=KEK_B)


async def test_kek_rotation_rewraps_and_sticks(session_factory):
    async with session_factory() as session:
        first = await bootstrap_keyring(session, kek_encoded=KEK_A)
    # Rotation window: new KEK current, old KEK previous. Boot re-wraps.
    async with session_factory() as session:
        rotated = await bootstrap_keyring(session, kek_encoded=KEK_B, previous_kek_encoded=KEK_A)
    assert rotated.dek(1) == first.dek(1)
    # After the window the previous KEK is dropped and boot still works,
    # proving the re-wrap was persisted.
    async with session_factory() as session:
        settled = await bootstrap_keyring(session, kek_encoded=KEK_B)
    assert settled.dek(1) == first.dek(1)


async def test_dek_rotation_reencrypts_and_retires(session_factory):
    async with session_factory() as session:
        keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
        secret = await _add_secret(session, keyring, "s1", {"password": "hunter2"})

        result = await rotate_dek(session, keyring)
        assert result == {"new_version": 2, "reencrypted": 1}
        assert keyring.active_version == 2

        refreshed = await session.get(Secret, secret.id)
        assert refreshed.key_version == 2
        plaintext = crypto.decrypt_value(
            keyring.dek(2),
            refreshed.nonce,
            refreshed.ciphertext,
            secret_id=refreshed.id,
            key_version=2,
        )
        assert plaintext == serialize_data({"password": "hunter2"})

        rows = (await session.execute(select(KeyVersion))).scalars().all()
        by_version = {r.version: r for r in rows}
        assert by_version[1].retired_at is not None
        assert by_version[2].retired_at is None


async def test_dek_rotation_survives_reboot(session_factory):
    async with session_factory() as session:
        keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
        await _add_secret(session, keyring, "s1", {"token": "tok"})
        await rotate_dek(session, keyring)
    async with session_factory() as session:
        rebooted = await bootstrap_keyring(session, kek_encoded=KEK_A)
        assert rebooted.active_version == 2
        secret = (await session.execute(select(Secret))).scalars().one()
        plaintext = crypto.decrypt_value(
            rebooted.dek(secret.key_version),
            secret.nonce,
            secret.ciphertext,
            secret_id=secret.id,
            key_version=secret.key_version,
        )
        assert plaintext == serialize_data({"token": "tok"})
