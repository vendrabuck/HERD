"""Direct router-function tests for app/routers/internal.py.

Same rationale as test_routers_direct.py: coverage.py under-attributes
post-await lines exercised only through the ASGI client (test_api.py's
test_internal_reveal_by_id / by_name / wrong_token / unknown_secret already
pin the observable behavior). These call the handlers directly so those
lines trace, and add the missing-token and internal_token_matches wiring
cases test_api.py does not reach.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from app.config import settings
from app.database import Base
from app.models import Secret
from app.routers.internal import (
    _check_internal_token,
    internal_reveal_by_id,
    internal_reveal_by_name,
)
from app.services.keyring import bootstrap_keyring
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PLAINTEXT = {"host": "10.0.0.5", "password": "hunter2-plaintext-canary"}


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def keyring(db_session):
    return await bootstrap_keyring(db_session, kek_encoded=settings.secrets_kek)


def _request(keyring) -> MagicMock:
    request = MagicMock()
    request.app.state.keyring = keyring
    return request


async def _create_secret(db_session, keyring, name: str = "frr-router-ssh") -> Secret:
    from app.routers.secrets import create_secret
    from app.schemas.secret import SecretCreate

    body = SecretCreate(name=name, type="password", data=PLAINTEXT)
    payload = {"sub": str(uuid.uuid4()), "role": "admin"}
    result = await create_secret(body, _request(keyring), payload, db_session)
    return await db_session.get(Secret, result.id)


# --- _check_internal_token ---


def test_check_internal_token_correct_token_is_a_noop():
    _check_internal_token(settings.internal_api_token)


def test_check_internal_token_wrong_token_raises_403():
    with pytest.raises(HTTPException) as exc:
        _check_internal_token("wrong-token")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid internal token"


def test_check_internal_token_empty_token_raises_403():
    with pytest.raises(HTTPException) as exc:
        _check_internal_token("")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid internal token"


# --- internal_reveal_by_id ---


async def test_internal_reveal_by_id_direct_returns_plaintext(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    result = await internal_reveal_by_id(
        row.id, _request(keyring), settings.internal_api_token, db_session
    )
    assert result.data == PLAINTEXT
    assert result.name == row.name


async def test_internal_reveal_by_id_direct_wrong_token_raises_before_lookup(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    with pytest.raises(HTTPException) as exc:
        await internal_reveal_by_id(row.id, _request(keyring), "wrong", db_session)
    assert exc.value.status_code == 403


async def test_internal_reveal_by_id_direct_not_found(db_session, keyring):
    with pytest.raises(HTTPException) as exc:
        await internal_reveal_by_id(
            uuid.uuid4(), _request(keyring), settings.internal_api_token, db_session
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Secret not found"


# --- internal_reveal_by_name ---


async def test_internal_reveal_by_name_direct_returns_plaintext(db_session, keyring):
    await _create_secret(db_session, keyring, "frr-router-ssh")
    result = await internal_reveal_by_name(
        "frr-router-ssh", _request(keyring), settings.internal_api_token, db_session
    )
    assert result.data == PLAINTEXT


async def test_internal_reveal_by_name_direct_wrong_token_raises_before_lookup(db_session, keyring):
    await _create_secret(db_session, keyring, "frr-router-ssh")
    with pytest.raises(HTTPException) as exc:
        await internal_reveal_by_name("frr-router-ssh", _request(keyring), "wrong", db_session)
    assert exc.value.status_code == 403


async def test_internal_reveal_by_name_direct_unknown_name_is_404(db_session, keyring):
    with pytest.raises(HTTPException) as exc:
        await internal_reveal_by_name(
            "does-not-exist", _request(keyring), settings.internal_api_token, db_session
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Secret not found"
