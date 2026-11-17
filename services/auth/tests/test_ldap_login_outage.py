"""`POST /login` in LDAP mode: a refusal and an outage get different answers (issue #1403).

The invariant: a login answer tells "your credentials are wrong" apart from "the
directory could not be asked", without revealing whether an account exists and
without turning an outage into a local-password fallback. So every answer the
directory gave about this login (no such user, a refused bind, an entry without the
email attribute) stays the 401 `Invalid credentials`, and every way the directory
could not be asked (incomplete configuration, a refused connection, a TLS failure,
a refused service-account bind, a search error or non-success result, a dropped or
slow user bind) is the 503 `Directory unavailable, credentials not checked`, with no
session, no account created, and no directory text in the body.

These run the real route, auth_service, and ldap_service code; only ldap3's
`Connection` is replaced, by the scripted directory in tests/_fake_directory.py.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.models.user import Role, User
from app.routers.auth import LOGIN_DIRECTORY_UNAVAILABLE_DETAIL
from app.services import auth_service, ldap_service
from ldap3.core.exceptions import (
    LDAPResponseTimeoutError,
    LDAPSocketOpenError,
    LDAPSocketReceiveError,
    LDAPStartTLSError,
)
from ldap3.core.results import (
    RESULT_BUSY,
    RESULT_INVALID_CREDENTIALS,
    RESULT_NO_SUCH_OBJECT,
    RESULT_SUCCESS,
    RESULT_UNWILLING_TO_PERFORM,
)
from sqlalchemy import func, select

from tests._fake_directory import FakeDirectory, alice_entry
from tests._harness import TestSessionLocal

PASSWORD = "correct-horse-battery"
UNAVAILABLE = {"detail": "Directory unavailable, credentials not checked"}
INVALID = {"detail": "Invalid credentials"}
# Text the scripted directory puts in its errors; none may reach a response.
DIRECTORY_TEXT = "secret-directory-detail"


@pytest.fixture(autouse=True)
def ldap_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    monkeypatch.setattr(settings, "ldap_bind_dn", "CN=svc,DC=example,DC=com", raising=False)
    monkeypatch.setattr(settings, "ldap_bind_password", "svc-pw", raising=False)
    monkeypatch.setattr(settings, "ldap_user_base_dn", "OU=Users,DC=example,DC=com", raising=False)
    monkeypatch.setattr(settings, "ldap_user_filter", "(sAMAccountName={username})", raising=False)
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail", raising=False)
    monkeypatch.setattr(settings, "ldap_username_attribute", "sAMAccountName", raising=False)
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)


@pytest.fixture
def directory(monkeypatch):
    fake = FakeDirectory(service_dn=settings.ldap_bind_dn)
    monkeypatch.setattr(ldap_service, "Connection", fake.connection)
    return fake


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def _login(client, login: str = "alice", password: str = PASSWORD):
    return await client.post("/login", json={"email": login, "password": password})


def test_the_detail_constant_is_the_documented_string():
    assert LOGIN_DIRECTORY_UNAVAILABLE_DETAIL == UNAVAILABLE["detail"]


_OUTAGES = {
    "service_connect_refused": {"service_bind": LDAPSocketOpenError(DIRECTORY_TEXT)},
    "service_tls_failure": {"service_start_tls": LDAPStartTLSError(DIRECTORY_TEXT)},
    "service_bind_refused": {"service_bind": RESULT_INVALID_CREDENTIALS},
    "search_transport_error": {"search": LDAPSocketReceiveError(DIRECTORY_TEXT)},
    "search_base_dn_missing": {"search": (RESULT_NO_SUCH_OBJECT, [])},
    "search_busy": {"search": (RESULT_BUSY, [])},
    "user_bind_connect_error": {"user_bind": LDAPSocketOpenError(DIRECTORY_TEXT)},
    "user_bind_slow_directory": {"user_bind": LDAPResponseTimeoutError(DIRECTORY_TEXT)},
    "user_bind_busy": {"user_bind": RESULT_BUSY},
}


@pytest.mark.parametrize("overrides", _OUTAGES.values(), ids=_OUTAGES.keys())
async def test_an_outage_is_503_with_the_fixed_detail_and_no_session(
    client, directory, monkeypatch, overrides, caplog
):
    monkeypatch.setattr(settings, "ldap_use_tls", "service_start_tls" in overrides, raising=False)
    for name, value in overrides.items():
        setattr(directory, name, value)
    with caplog.at_level("INFO"):
        response = await _login(client)
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert "www-authenticate" not in response.headers
    assert DIRECTORY_TEXT not in response.text
    assert await _user_count() == 0
    failures = [r for r in caplog.records if getattr(r, "action", None) == "login_failure"]
    assert [(r.levelname, r.reason, r.source) for r in failures] == [
        ("WARNING", "directory_unavailable", "ldap")
    ]
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()
        assert PASSWORD not in str(record.__dict__)


@pytest.mark.parametrize("setting", ["ldap_server_url", "ldap_user_base_dn"])
async def test_incomplete_configuration_is_503(client, directory, monkeypatch, setting):
    monkeypatch.setattr(settings, setting, "", raising=False)
    response = await _login(client)
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert directory.built == []


_REFUSALS = {
    "user_not_found": {"search": (RESULT_SUCCESS, [])},
    "wrong_password": {"user_bind": RESULT_INVALID_CREDENTIALS},
    "account_refused_by_directory": {"user_bind": RESULT_UNWILLING_TO_PERFORM},
    "missing_email": {"search": (RESULT_SUCCESS, [alice_entry(mail=[])])},
}


@pytest.mark.parametrize("overrides", _REFUSALS.values(), ids=_REFUSALS.keys())
async def test_what_the_directory_said_stays_the_401(client, directory, overrides):
    for name, value in overrides.items():
        setattr(directory, name, value)
    response = await _login(client)
    assert (response.status_code, response.json()) == (401, INVALID)
    assert response.headers["www-authenticate"] == "Bearer"
    assert await _user_count() == 0


async def test_an_empty_password_is_the_401_before_any_directory_call(client, directory):
    response = await _login(client, password="")
    assert (response.status_code, response.json()) == (401, INVALID)
    assert directory.built == []


async def test_a_reachable_directory_still_signs_in(client, directory):
    response = await _login(client)
    assert response.status_code == 200, response.text
    assert await _user_count() == 1


async def test_an_outage_never_falls_back_to_a_local_password(client, directory):
    """IAM-LDAP-1: with the directory down, an active local superadmin typing the
    right local password gets the outage answer, never a session."""
    async with TestSessionLocal() as db:
        await auth_service.create_user(db, "root@example.com", "root", PASSWORD)
        user = (await db.execute(select(User).where(User.username == "root"))).scalar_one()
        user.role = Role.SUPERADMIN
        await db.commit()
    directory.service_bind = LDAPSocketOpenError(DIRECTORY_TEXT)
    for login in ("root@example.com", "root"):
        response = await _login(client, login=login)
        assert (response.status_code, response.json()) == (503, UNAVAILABLE)
        assert "access_token" not in response.text
