"""`POST /login` in OIDC mode, the break-glass switch (ADR 0016 D5, phase 3).

With OIDC_BREAK_GLASS_LOCAL_SUPERADMIN off, every account is refused. With it on,
exactly one class is admitted: an active local superadmin with the right password.
Every refusal answers the same 401 `Invalid credentials` with
`WWW-Authenticate: Bearer`, and every answer costs exactly one bcrypt: a spy on
`auth_service.verify_password` records the hash each call checked, so the matrix
pins that each refusal ran against the dummy hash, except a wrong password on the
eligible account, which ran against that account's own hash (the work its success
costs). Also pinned: the switch is inert outside OIDC mode, refresh keeps working
in OIDC mode, and the lifespan's startup warning fires only when the switch is on
in OIDC mode.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings
from app.models.user import Role, User
from app.services import auth_service
from app.services.auth_service import (
    FederatedLogin,
    create_ldap_user,
    create_oidc_user,
    create_user,
    set_user_active,
)

from tests._harness import TestSessionLocal

PASSWORD = "right-password-1"
INVALID = {"detail": "Invalid credentials"}
LOGGER = "app.services.auth_service"


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.fixture
def oidc_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)


@pytest.fixture
def break_glass(monkeypatch):
    def _set(on: bool) -> None:
        monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", on)

    return _set


@pytest.fixture
def bcrypt_calls(monkeypatch) -> list[str]:
    """The hash every `verify_password` call checked, in order."""
    calls: list[str] = []
    real = auth_service.verify_password

    def spy(plain: str, hashed: str) -> bool:
        calls.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    return calls


async def _account(kind: str) -> tuple[str, str | None]:
    """Create one account of `kind`; return its login email and stored hash."""
    async with TestSessionLocal() as db:
        if kind in ("superadmin", "inactive-superadmin", "admin", "user"):
            role = {"admin": Role.ADMIN, "user": Role.USER}.get(kind, Role.SUPERADMIN)
            user = await create_user(db, f"{kind}@example.test", kind, PASSWORD, role=role)
            if kind == "inactive-superadmin":
                await set_user_active(db, user.id, False)
        elif kind == "ldap":
            user = await create_ldap_user(db, "ldap@example.test", "ldapuser")
        elif kind == "oidc":
            user = await create_oidc_user(
                db,
                FederatedLogin(
                    issuer="https://idp.example.test",
                    subject="sub-1",
                    email="oidc@example.test",
                    email_verified=True,
                    username="oidcuser",
                    full_name=None,
                ),
            )
        elif kind == "local-superadmin-without-hash":
            user = await create_user(
                db, f"{kind}@example.test", kind, PASSWORD, role=Role.SUPERADMIN
            )
            user.hashed_password = None
            await db.commit()
        elif kind in ("ldap-superadmin-with-hash", "oidc-superadmin-with-hash"):
            # Cannot arise through the API (superadmin is never assigned, and
            # ldap and oidc accounts carry no hash); built directly so that the
            # auth_source check is the only one that can refuse it.
            user = User(
                email=f"{kind}@example.test",
                username=kind,
                full_name=kind,
                hashed_password=auth_service.get_password_hash(PASSWORD),
                auth_source=kind.split("-")[0],
                role=Role.SUPERADMIN,
            )
            db.add(user)
            await db.commit()
        else:
            return "nobody@example.test", None
        return user.email, user.hashed_password


async def _login(client, email: str, password: str = PASSWORD):
    return await client.post("/login", json={"email": email, "password": password})


def _assert_invalid(response) -> None:
    assert response.status_code == 401
    assert response.json() == INVALID
    assert response.headers["www-authenticate"] == "Bearer"


def _reasons(caplog) -> list[str]:
    return [
        r.reason
        for r in caplog.records
        if r.name == LOGGER and getattr(r, "action", None) == "login_failure"
    ]


async def test_switch_off_refuses_the_local_superadmin_without_reading_the_database(
    monkeypatch, client, oidc_mode, break_glass, bcrypt_calls, caplog
):
    email, _ = await _account("superadmin")
    break_glass(False)

    async def no_lookup(*args, **kwargs):
        raise AssertionError("the switch is off; no account may be looked up")

    monkeypatch.setattr(auth_service, "get_user_by_email", no_lookup)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _assert_invalid(await _login(client, email))
    assert bcrypt_calls == [auth_service._DUMMY_HASH]
    assert _reasons(caplog) == ["password_login_disabled"]


async def test_switch_on_admits_an_active_local_superadmin(
    client, oidc_mode, break_glass, bcrypt_calls, caplog
):
    email, stored_hash = await _account("superadmin")
    break_glass(True)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = await _login(client, email)
    assert response.status_code == 200
    assert sorted(response.json()) == ["access_token", "refresh_token", "token_type"]
    assert bcrypt_calls == [stored_hash]
    me = await client.get(
        "/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    assert (me.json()["email"], me.json()["role"]) == (email, "superadmin")
    [record] = [r for r in caplog.records if getattr(r, "action", None) == "oidc_break_glass_login"]
    assert record.levelno == logging.WARNING


@pytest.mark.parametrize(
    "kind",
    [
        "admin",
        "user",
        "oidc",
        "ldap",
        "inactive-superadmin",
        "unknown-email",
        "ldap-superadmin-with-hash",
        "oidc-superadmin-with-hash",
        "local-superadmin-without-hash",
    ],
)
async def test_switch_on_refuses_every_other_account_after_the_dummy_hash(
    client, oidc_mode, break_glass, bcrypt_calls, caplog, kind
):
    """The ADR's break-glass matrix: each is the same 401, after exactly one bcrypt
    against the dummy hash, the account's own hash never checked."""
    await _account("superadmin")
    email, _ = await _account(kind)
    break_glass(True)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _assert_invalid(await _login(client, email))
    assert bcrypt_calls == [auth_service._DUMMY_HASH]
    assert _reasons(caplog) == ["break_glass_not_eligible"]


async def test_switch_on_refuses_a_wrong_password_after_one_bcrypt_on_its_own_hash(
    client, oidc_mode, break_glass, bcrypt_calls, caplog
):
    email, stored_hash = await _account("superadmin")
    break_glass(True)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _assert_invalid(await _login(client, email, "wrong-password-1"))
    assert bcrypt_calls == [stored_hash]
    assert _reasons(caplog) == ["wrong_password"]


async def test_switch_is_inert_in_local_mode(client, break_glass, bcrypt_calls):
    """Outside OIDC mode the setting changes nothing: a local user still signs in."""
    email, stored_hash = await _account("user")
    break_glass(True)
    assert settings.auth_method == "local"
    response = await _login(client, email)
    assert response.status_code == 200
    assert bcrypt_calls == [stored_hash]


async def test_refresh_keeps_working_in_oidc_mode(monkeypatch, client, break_glass):
    """IAM-USER-9 analog (ADR 0016 D5): refresh ignores the mode, so a session begun
    before the switch survives it."""
    email, _ = await _account("user")
    issued = (await _login(client, email)).json()
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    break_glass(False)
    refreshed = await client.post("/refresh", json={"refresh_token": issued["refresh_token"]})
    assert refreshed.status_code == 200


@pytest.mark.parametrize(
    "mode,on,fires",
    [
        ("local", False, False),
        ("local", True, False),
        ("ldap", True, False),
        ("oidc", False, False),
        ("oidc", True, True),
    ],
)
async def test_lifespan_warns_about_break_glass_only_when_on_in_oidc_mode(
    monkeypatch, caplog, mode, on, fires
):
    """Carried requirement 2: the lifespan calls log_oidc_startup_warnings, so the
    break-glass warning (and, in OIDC mode, the incomplete-settings one) is logged
    at startup."""
    from app.main import lifespan

    monkeypatch.setattr(settings, "auth_method", mode, raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", on)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    with (
        patch("app.main.create_all_and_stamp", AsyncMock()),
        patch("app.main._seed_superadmin", AsyncMock()),
        patch("app.main._seed_not_grouped", AsyncMock()),
        patch("app.main.ldap_group_sync_loop_enabled", return_value=False),
        caplog.at_level(logging.WARNING, logger="app.config"),
    ):
        async with lifespan(MagicMock()):
            pass
    actions = [getattr(r, "action", None) for r in caplog.records if r.name == "app.config"]
    assert ("oidc_break_glass_enabled" in actions) is fires
    assert ("oidc_settings_incomplete" in actions) is (mode == "oidc")
