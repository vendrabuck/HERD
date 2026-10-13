"""verify_access_token and its two callers: the one-element algorithm list and the
mapping of every jose error to JWTError, so the callers answer 401.

The three token shapes pinned at each layer: an unsigned `alg: none` token, a
token signed with HS512 while the service is set to HS256, and a correct token.
A key jose cannot build an HMAC key from (shaped like a public key) raises
JWKError, which is a JOSEError and not a JWTError; verify_access_token raises it
as JWTError.
"""

import base64
import json
import uuid

import pytest
from app.config import settings
from app.dependencies.auth import _ClaimAvailability, _decode_role_claim, get_current_user
from app.services.auth_service import create_user
from app.utils.jwt import create_access_token, verify_access_token
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import JWTError, jwt
from starlette.requests import Request

from tests._harness import TestSessionLocal

ASYMMETRIC_SHAPED_KEY = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQc=\n"


def _unsigned_token(payload: dict) -> str:
    def _segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(payload)}."


def _hs512_token(payload: dict) -> str:
    return jwt.encode(payload, settings.secret_key, algorithm="HS512")


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _request(token: str) -> Request:
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


def test_the_service_algorithm_is_hs256_in_this_suite():
    assert settings.algorithm == "HS256"


# --- verify_access_token ---


def test_verify_accepts_a_correct_token():
    sub = str(uuid.uuid4())
    assert verify_access_token(create_access_token({"sub": sub}))["sub"] == sub


def test_verify_refuses_alg_none():
    with pytest.raises(JWTError):
        verify_access_token(_unsigned_token({"sub": str(uuid.uuid4()), "role": "superadmin"}))


def test_verify_refuses_another_hmac_algorithm():
    with pytest.raises(JWTError):
        verify_access_token(_hs512_token({"sub": str(uuid.uuid4())}))


def test_verify_raises_a_key_error_as_jwterror(monkeypatch):
    token = create_access_token({"sub": str(uuid.uuid4())})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    with pytest.raises(JWTError) as exc_info:
        verify_access_token(token)
    assert type(exc_info.value) is JWTError
    assert type(exc_info.value.__cause__).__name__ == "JWKError"


# --- get_current_user ---


async def _make_user():
    async with TestSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        return await create_user(db, f"tv-{suffix}@test.com", f"tv-{suffix}", "password123")


@pytest.mark.asyncio
async def test_get_current_user_accepts_a_correct_token():
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    async with TestSessionLocal() as db:
        found = await get_current_user(credentials=_creds(token), db=db)
    assert found.id == user.id


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["alg-none", "hs512"])
async def test_get_current_user_refuses_with_401(shape):
    user = await _make_user()
    payload = {"sub": str(user.id), "role": "user"}
    token = _unsigned_token(payload) if shape == "alg-none" else _hs512_token(payload)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=_creds(token), db=db)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


@pytest.mark.asyncio
async def test_get_current_user_answers_401_for_a_key_jose_cannot_use(monkeypatch):
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=_creds(token), db=db)
    assert exc_info.value.status_code == 401


# --- the role-claim read ---


@pytest.mark.asyncio
async def test_role_claim_read_accepts_a_correct_token():
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "admin"})
    assert await _decode_role_claim(_request(token)) == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["alg-none", "hs512"])
async def test_role_claim_read_is_undecodable(shape):
    payload = {"sub": str(uuid.uuid4()), "role": "superadmin"}
    token = _unsigned_token(payload) if shape == "alg-none" else _hs512_token(payload)
    assert await _decode_role_claim(_request(token)) is _ClaimAvailability.UNDECODABLE


@pytest.mark.asyncio
async def test_role_claim_read_is_undecodable_for_a_key_jose_cannot_use(monkeypatch):
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "superadmin"})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    assert await _decode_role_claim(_request(token)) is _ClaimAvailability.UNDECODABLE


# --- over HTTP ---


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["alg-none", "hs512"])
async def test_me_refuses_with_401(make_client, shape):
    user = await _make_user()
    payload = {"sub": str(user.id), "role": "user"}
    token = _unsigned_token(payload) if shape == "alg-none" else _hs512_token(payload)
    async with make_client() as client:
        resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"


@pytest.mark.asyncio
async def test_me_answers_200_for_a_correct_token(make_client):
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    async with make_client() as client:
        resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["id"] == str(user.id)


@pytest.mark.asyncio
async def test_me_answers_401_for_a_key_jose_cannot_use(make_client, monkeypatch):
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    async with make_client() as client:
        resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
