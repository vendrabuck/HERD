"""The login password bound is the bound of the mode that judges it (issue #1404).

Local mode and the OIDC break-glass login check the password against a bcrypt
hash, which reads at most 72 bytes, so they keep registration's 72-character bound
exactly: a longer password is the same 422 it always was and never reaches the
check. LDAP mode sends the password to the directory in a bind and never hashes
it (IAM-LDAP-1: every login in LDAP mode goes to the directory, a local account
included), so it takes up to 1024 characters, a ceiling on the request only, and
passes the password through unchanged. NUL and lone-surrogate handling is IAM-LOGIN-5's
and does not change.
"""

from __future__ import annotations

import json
import typing

import pytest
from app.config import AuthMethod, settings
from app.routers import auth as auth_router
from app.schemas import auth as schemas
from app.services import ldap_service
from app.services.auth_service import create_user

from tests._harness import TestSessionLocal

JSON = {"content-type": "application/json"}


def _too_long(limit: int) -> dict:
    return {
        "type": "string_too_long",
        "loc": ["body", "password"],
        "msg": f"String should have at most {limit} characters",
    }


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.fixture
def judge_calls(monkeypatch):
    """Record every call that reaches `authenticate_user`, the mode's judge."""
    calls: list[str] = []
    real = auth_router.authenticate_user

    async def _recording(db, email, password):
        calls.append(password)
        return await real(db, email, password)

    monkeypatch.setattr(auth_router, "authenticate_user", _recording)
    return calls


@pytest.fixture
def ldap_mode(monkeypatch):
    """LDAP mode with the directory stubbed: it accepts exactly `passwords[0]`."""
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    seen: list[str] = []

    async def _bind(username, password):
        seen.append(password)
        if password == seen[0]:
            return ldap_service.LdapIdentity(username="alice", email="alice@example.com", dn="x")
        return None

    monkeypatch.setattr(ldap_service, "bind_user", _bind)
    return seen


async def _login(client, password: str, email: str = "alice"):
    body = json.dumps({"email": email, "password": password}).encode()
    return await client.post("/login", content=body, headers=JSON)


# -- 1. The bound per mode -------------------------------------------------------------


def test_every_mode_has_its_judges_bound():
    """Every AuthMethod value is decided, and only LDAP mode leaves bcrypt's 72."""
    bounds = {mode: schemas.login_password_max_length(mode) for mode in typing.get_args(AuthMethod)}
    assert bounds == {"local": 72, "ldap": 1024, "oidc": 72}
    assert schemas.PASSWORD_MAX_LENGTH == 72
    assert schemas.LDAP_LOGIN_PASSWORD_MAX_LENGTH == 1024


def test_the_ldap_bound_never_exceeds_the_hashing_bound():
    """1024 characters are at most 4096 UTF-8 bytes, the most `validate_password`
    takes, so no password the LDAP bound admits is one hashing would refuse."""
    from app.services.passwords import MAX_PASSWORD_BYTES

    assert schemas.LDAP_LOGIN_PASSWORD_MAX_LENGTH * 4 <= MAX_PASSWORD_BYTES


def test_registration_keeps_72():
    assert schemas.RegisterRequest.model_fields["password"].metadata[-1].max_length == 72


# -- 2. LDAP mode: the directory judges, up to 1024 characters ------------------------


@pytest.mark.parametrize("length", [73, 100, 1024])
async def test_ldap_mode_passes_a_long_password_to_the_directory_unchanged(
    client, ldap_mode, length
):
    password = ("pass-phrase-" * 100)[:length]
    response = await _login(client, password)
    assert response.status_code == 200, response.text
    assert ldap_mode == [password]


async def test_ldap_mode_takes_1024_four_byte_characters(client, ldap_mode):
    """The bound counts characters, as every request bound here does: 1024
    four-byte characters (4096 UTF-8 bytes) still reach the directory whole."""
    password = "\U0001f511" * 1024
    response = await _login(client, password)
    assert response.status_code == 200, response.text
    assert ldap_mode == [password]


async def test_ldap_mode_refuses_past_1024_before_the_directory(client, ldap_mode, judge_calls):
    password = "p" * 1025
    response = await _login(client, password)
    assert (response.status_code, response.json()) == (422, {"detail": [_too_long(1024)]})
    assert password not in response.text
    assert ldap_mode == [] and judge_calls == []


async def test_ldap_mode_still_passes_a_nul_to_the_directory(client, ldap_mode):
    """IAM-LOGIN-5 unchanged: LDAP mode passes a NUL through, now past 72 too."""
    password = "a" * 90 + "\x00" + "b" * 9
    response = await _login(client, password)
    assert response.status_code == 200, response.text
    assert ldap_mode == [password]


async def test_ldap_mode_a_long_lone_surrogate_is_still_string_unicode(client, ldap_mode):
    response = await client.post(
        "/login",
        content=('{"email": "alice", "password": "' + "a" * 100 + '\\ud800"}').encode(),
        headers=JSON,
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("string_unicode", ["body", "password"])
    assert ldap_mode == []


# -- 3. Local mode and the break-glass login: bcrypt judges, 72 exactly ----------------


async def test_local_mode_refuses_73_with_the_unchanged_422_before_bcrypt(client, judge_calls):
    response = await _login(client, "p" * 73, email="root@example.com")
    assert (response.status_code, response.json()) == (422, {"detail": [_too_long(72)]})
    assert judge_calls == []


async def test_local_mode_past_1024_is_refused_at_the_ceiling_of_every_mode(client, judge_calls):
    """Known and accepted: the schema's `max_length` is the ceiling of every mode
    (LDAP's 1024) and runs before the mode's own bound, so a local-mode password
    over 1024 is told 1024. Still refused before bcrypt."""
    response = await _login(client, "p" * 2000, email="root@example.com")
    assert (response.status_code, response.json()) == (422, {"detail": [_too_long(1024)]})
    assert judge_calls == []


@pytest.mark.parametrize("mode", ["local", "ldap", "oidc"])
async def test_a_long_lone_surrogate_is_string_unicode_in_every_mode(client, monkeypatch, mode):
    """IAM-LOGIN-5 unchanged: the surrogate refusal comes before any length bound,
    as it did under the old `max_length=72`."""
    monkeypatch.setattr(settings, "auth_method", mode, raising=False)
    for length in (80, 2000):
        response = await client.post(
            "/login",
            content=('{"email": "alice", "password": "' + "a" * length + '\\ud800"}').encode(),
            headers=JSON,
        )
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert (error["type"], error["loc"]) == ("string_unicode", ["body", "password"])


async def test_local_mode_signs_in_with_72(client, judge_calls):
    password = ("local-pass-" * 10)[:72]
    async with TestSessionLocal() as db:
        await create_user(db, "root@example.com", "root", password)
    response = await _login(client, password, email="root@example.com")
    assert response.status_code == 200, response.text
    assert judge_calls == [password]


async def test_local_mode_422_matches_registrations(client):
    """Local login refuses a long password with exactly registration's error."""
    login = await _login(client, "p" * 73, email="root@example.com")
    register = await client.post(
        "/register",
        json={"email": "new@example.com", "username": "newuser", "password": "p" * 73},
    )
    assert login.json()["detail"] == register.json()["detail"]


async def test_oidc_break_glass_refuses_73_before_bcrypt(client, judge_calls, monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", True, raising=False)
    response = await _login(client, "p" * 73, email="root@example.com")
    assert (response.status_code, response.json()) == (422, {"detail": [_too_long(72)]})
    assert judge_calls == []


async def test_oidc_break_glass_takes_72(client, judge_calls, monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", True, raising=False)
    response = await _login(client, "p" * 72, email="root@example.com")
    assert response.status_code == 401  # no such superadmin; it reached the judge
    assert judge_calls == ["p" * 72]
