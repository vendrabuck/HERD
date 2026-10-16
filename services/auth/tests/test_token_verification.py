"""verify_access_token and its two callers: the one-element algorithm list and the
mapping of every PyJWT error to InvalidTokenError, so the callers answer 401.

The three token shapes pinned at each layer: an unsigned `alg: none` token, a
token signed with HS512 while the service is set to HS256, and a correct token.
A key PyJWT cannot prepare an HMAC key from (a PEM public key block) raises
InvalidKeyError, which is a PyJWTError and not an InvalidTokenError;
verify_access_token raises it as InvalidTokenError.
"""

import base64
import json
import logging
import uuid

import jwt
import pytest
from app.config import settings
from app.dependencies.auth import _ClaimAvailability, _decode_role_claim, get_current_user
from app.services.auth_service import create_user
from app.utils.jwt import create_access_token, verify_access_token
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from herd_common import jwt_codec
from herd_common.logging import JSONFormatter
from starlette.requests import Request

from tests._harness import TestSessionLocal

ASYMMETRIC_SHAPED_KEY = (
    "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQc=\n-----END PUBLIC KEY-----\n"
)


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
    with pytest.raises(jwt.InvalidTokenError):
        verify_access_token(_unsigned_token({"sub": str(uuid.uuid4()), "role": "superadmin"}))


def test_verify_refuses_another_hmac_algorithm():
    with pytest.raises(jwt.InvalidTokenError):
        verify_access_token(_hs512_token({"sub": str(uuid.uuid4())}))


def test_verify_raises_a_key_error_as_invalid_token_error(monkeypatch):
    token = create_access_token({"sub": str(uuid.uuid4())})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    with pytest.raises(jwt.InvalidTokenError) as exc_info:
        verify_access_token(token)
    assert type(exc_info.value) is jwt.InvalidTokenError
    assert str(exc_info.value) == "token could not be verified"
    assert type(exc_info.value.__cause__) is jwt.InvalidKeyError


def test_verify_gives_decode_exactly_the_configured_algorithm(monkeypatch):
    seen: list = []
    real_decode = jwt_codec.jwt.decode

    def recording_decode(*args, **kwargs):
        seen.append(kwargs.get("algorithms"))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(jwt_codec.jwt, "decode", recording_decode)
    verify_access_token(create_access_token({"sub": str(uuid.uuid4())}))
    with pytest.raises(jwt.InvalidTokenError):
        verify_access_token(_unsigned_token({"sub": str(uuid.uuid4())}))
    assert seen == [[settings.algorithm], [settings.algorithm]]


def test_verify_logs_a_key_error_once_by_class_name(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="app.utils.jwt")
    token = create_access_token({"sub": str(uuid.uuid4())})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    with pytest.raises(jwt.InvalidTokenError):
        verify_access_token(token)
    records = [r for r in caplog.records if r.name == "app.utils.jwt"]
    assert len(records) == 1
    formatted = JSONFormatter("auth").format(records[0])
    logged = json.loads(formatted)
    assert logged["level"] == "ERROR"
    assert logged["message"] == (
        "access token not checked: the configured key is unusable (InvalidKeyError)"
    )
    assert logged["action"] == "jwt_key_unusable"
    assert logged["error_class"] == "InvalidKeyError"
    assert "BEGIN PUBLIC KEY" not in formatted


@pytest.mark.parametrize("shape", ["alg-none", "hs512"])
def test_verify_does_not_log_a_token_that_does_not_verify(caplog, shape):
    caplog.set_level(logging.DEBUG, logger="app.utils.jwt")
    payload = {"sub": str(uuid.uuid4())}
    token = _unsigned_token(payload) if shape == "alg-none" else _hs512_token(payload)
    with pytest.raises(jwt.InvalidTokenError):
        verify_access_token(token)
    assert [r for r in caplog.records if r.name == "app.utils.jwt"] == []


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
async def test_get_current_user_answers_401_for_a_key_pyjwt_cannot_use(monkeypatch):
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=_creds(token), db=db)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


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
async def test_role_claim_read_is_undecodable_for_a_key_pyjwt_cannot_use(monkeypatch):
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
async def test_me_answers_401_for_a_key_pyjwt_cannot_use(make_client, monkeypatch):
    user = await _make_user()
    token = create_access_token({"sub": str(user.id), "role": "user"})
    monkeypatch.setattr(settings, "secret_key", ASYMMETRIC_SHAPED_KEY)
    async with make_client() as client:
        resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"
