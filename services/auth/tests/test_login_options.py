"""`GET /login-options` in every mode (ADR 0016 D10, phase 3).

The login page's only source for the active sign-in method: answered from auth's
live settings, public, never cached, with no database read. Each case asserts the
whole body, so a field that drifts in any mode fails here.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.database import get_db
from app.main import app


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


def _no_db():
    async def refuse():
        raise AssertionError("GET /login-options opened a database session")
        yield  # pragma: no cover

    return refuse


CASES = [
    (
        "local",
        False,
        {"auth_method": "local", "registration_open": True, "password_login": True, "sso": None},
    ),
    (
        "local",
        True,
        {"auth_method": "local", "registration_open": True, "password_login": True, "sso": None},
    ),
    (
        "ldap",
        False,
        {"auth_method": "ldap", "registration_open": False, "password_login": True, "sso": None},
    ),
    (
        "ldap",
        True,
        {"auth_method": "ldap", "registration_open": False, "password_login": True, "sso": None},
    ),
    (
        "oidc",
        False,
        {
            "auth_method": "oidc",
            "registration_open": False,
            "password_login": False,
            "sso": {"provider_name": "Example IdP"},
        },
    ),
    (
        "oidc",
        True,
        {
            "auth_method": "oidc",
            "registration_open": False,
            "password_login": True,
            "sso": {"provider_name": "Example IdP"},
        },
    ),
]


@pytest.mark.parametrize(
    "mode,break_glass,expected",
    CASES,
    ids=[f"{mode}-break-glass-{'on' if on else 'off'}" for mode, on, _ in CASES],
)
async def test_login_options_per_mode(monkeypatch, client, mode, break_glass, expected):
    """The ADR's per-mode table; `password_login` follows the break-glass setting
    in OIDC mode only, and the setting is inert in the other two modes."""
    monkeypatch.setattr(settings, "auth_method", mode, raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", break_glass)
    monkeypatch.setattr(settings, "oidc_provider_name", "Example IdP")
    app.dependency_overrides[get_db] = _no_db()
    response = await client.get("/login-options")
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["cache-control"] == "no-store"


async def test_sso_is_offered_in_oidc_mode_even_with_incomplete_settings(monkeypatch, client):
    """ADR 0016 D10: the button still renders; the begin route then answers
    `unavailable` (pinned in test_oidc_routes.py)."""
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    monkeypatch.setattr(settings, "oidc_client_secret", "")
    monkeypatch.setattr(settings, "oidc_provider_name", "single sign-on")
    response = await client.get("/login-options")
    assert response.json()["sso"] == {"provider_name": "single sign-on"}


async def test_login_options_is_public_and_ignores_a_bad_bearer(client):
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        response = await client.get("/login-options", headers=headers)
        assert response.status_code == 200
        assert response.json()["auth_method"] == settings.auth_method
