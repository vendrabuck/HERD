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
from app.services import keyring as keyring_module
from app.services.crypto import KekError
from app.services.keyring import (
    DEK_ROTATION_LOCK_KEY,
    Keyring,
    KeyVersionUnavailableError,
    RotationConflictError,
    bootstrap_keyring,
    rotate_dek,
    serialize_data,
)
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


# Issue #1085: a rotation in one replica binds every replica, and two
# rotations never end in a 500.


def _stale_replica(keyring: Keyring) -> Keyring:
    """A second process booted before the rotation: same KEK, version 1 only."""
    return Keyring(keyring._kek, {1: keyring.dek(1)}, active_version=1)


async def test_peer_replica_loads_a_rotated_version_on_first_use(session_factory):
    async with session_factory() as session:
        replica_a = await bootstrap_keyring(session, kek_encoded=KEK_A)
        replica_b = _stale_replica(replica_a)
        await _add_secret(session, replica_a, "s1", {"password": "hunter2"})
        await rotate_dek(session, replica_a)

    async with session_factory() as session:
        secret = (await session.execute(select(Secret))).scalars().one()
        assert secret.key_version == 2
        dek = await replica_b.load_dek(session, 2)
        assert dek == replica_a.dek(2)
        plaintext = crypto.decrypt_value(
            dek, secret.nonce, secret.ciphertext, secret_id=secret.id, key_version=2
        )
        assert plaintext == serialize_data({"password": "hunter2"})
        # Cached after the first load: no row needed the second time.
        assert replica_b.dek(2) == dek


async def test_peer_replica_encrypts_new_data_under_the_rotated_version(session_factory):
    async with session_factory() as session:
        replica_a = await bootstrap_keyring(session, kek_encoded=KEK_A)
        replica_b = _stale_replica(replica_a)
        await rotate_dek(session, replica_a)

    async with session_factory() as session:
        version, dek = await replica_b.current(session)
    assert version == 2
    assert dek == replica_a.dek(2)
    assert replica_b.active_version == 2


async def test_load_dek_unknown_version_is_unavailable(session_factory):
    async with session_factory() as session:
        keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
        with pytest.raises(KeyVersionUnavailableError, match="^key version 99 is not available$"):
            await keyring.load_dek(session, 99)


async def test_load_dek_under_a_different_kek_is_unavailable(session_factory):
    async with session_factory() as session:
        replica_a = await bootstrap_keyring(session, kek_encoded=KEK_A)
        await rotate_dek(session, replica_a)
    wrong_kek = Keyring(crypto.load_kek(KEK_B), {1: replica_a.dek(1)}, active_version=1)
    async with session_factory() as session:
        with pytest.raises(KeyVersionUnavailableError) as info:
            await wrong_kek.load_dek(session, 2)
    assert info.value.version == 2
    assert 2 not in wrong_kek._deks


async def test_stale_replica_rotation_picks_the_next_version(session_factory):
    async with session_factory() as session:
        replica_a = await bootstrap_keyring(session, kek_encoded=KEK_A)
        replica_b = _stale_replica(replica_a)
        await _add_secret(session, replica_a, "s1", {"token": "tok"})
        assert (await rotate_dek(session, replica_a))["new_version"] == 2

    # replica_b never saw version 2; its rotation must read the table, not
    # its memory, and decrypt the version 2 row through load_dek.
    async with session_factory() as session:
        result = await rotate_dek(session, replica_b)
    assert result == {"new_version": 3, "reencrypted": 1}

    async with session_factory() as session:
        secret = (await session.execute(select(Secret))).scalars().one()
        assert secret.key_version == 3
        plaintext = crypto.decrypt_value(
            await replica_a.load_dek(session, 3),
            secret.nonce,
            secret.ciphertext,
            secret_id=secret.id,
            key_version=3,
        )
        assert plaintext == serialize_data({"token": "tok"})
        rows = (await session.execute(select(KeyVersion))).scalars().all()
        assert {r.version: r.retired_at is None for r in rows} == {1: False, 2: False, 3: True}


async def test_rotation_takes_the_lock_before_reading_the_key_table(session_factory, monkeypatch):
    calls: list[str] = []

    async def fake_lock(session, key):
        calls.append(f"lock:{key}")

    monkeypatch.setattr(keyring_module, "xact_lock", fake_lock)
    async with session_factory() as session:
        keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
        real_execute = session.execute

        async def spy_execute(*args, **kwargs):
            calls.append("read")
            return await real_execute(*args, **kwargs)

        monkeypatch.setattr(session, "execute", spy_execute)
        await rotate_dek(session, keyring)
    assert calls[0] == f"lock:{DEK_ROTATION_LOCK_KEY}"
    assert calls[1] == "read"


async def test_concurrent_rotation_conflict_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    """The backstop: another rotation commits the same new version between
    this rotation's read and its insert (a database without advisory locks).
    The loser raises RotationConflictError, rolls back, and every secret
    still decrypts under its old version."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            keyring = await bootstrap_keyring(session, kek_encoded=KEK_A)
            secret = await _add_secret(session, keyring, "s1", {"password": "pw"})

        async with factory() as session:
            real_execute = session.execute
            raced = False

            async def racing_execute(*args, **kwargs):
                nonlocal raced
                result = await real_execute(*args, **kwargs)
                if not raced:
                    raced = True
                    async with factory() as rival:
                        rival.add(
                            KeyVersion(
                                version=2,
                                wrapped_dek=crypto.wrap_dek(keyring._kek, crypto.generate_dek(), 2),
                            )
                        )
                        await rival.commit()
                return result

            monkeypatch.setattr(session, "execute", racing_execute)
            with pytest.raises(RotationConflictError):
                await rotate_dek(session, keyring)
        assert keyring.active_version == 1
        assert 2 not in keyring._deks

        async with factory() as session:
            stored = await session.get(Secret, secret.id)
            assert stored.key_version == 1
            plaintext = crypto.decrypt_value(
                keyring.dek(1),
                stored.nonce,
                stored.ciphertext,
                secret_id=stored.id,
                key_version=1,
            )
            assert plaintext == serialize_data({"password": "pw"})
            rows = (await session.execute(select(KeyVersion))).scalars().all()
            assert {r.version: r.retired_at is None for r in rows} == {1: True, 2: True}
    finally:
        await engine.dispose()
