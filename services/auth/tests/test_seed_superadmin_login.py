"""The seeded superadmin signs in with the value the operator configured (issue #1450).

The invariant of IAM-BOOT-3: every SUPERADMIN_PASSWORD the startup seed accepts
is a password the real `POST /login` route answers 200 for, in local mode and at
the single sign-on break-glass login. Before #1450 the seed hashed a value of
any length, and login in those two modes refuses more than 72 characters with a
422 before the password is checked. The values are the seed's accepted
boundary values (tests/_seed_values.py), each seeded through `_seed_superadmin`
itself and then typed at the route.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from app.config import settings

from tests._harness import TestSessionLocal
from tests._seed_values import ACCEPTED_SEED_VALUES

JSON = {"content-type": "application/json"}
EMAIL = "root@example.com"

VALUES = [pytest.param(value, typed, id=name) for name, value, typed in ACCEPTED_SEED_VALUES]


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _seed(value: str) -> None:
    from app.main import _seed_superadmin

    with (
        patch("app.main.settings") as seed_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        seed_settings.superadmin_email = EMAIL
        seed_settings.superadmin_username = "root"
        seed_settings.superadmin_password = value
        await _seed_superadmin()


async def _login(client, password: str):
    body = json.dumps({"email": EMAIL, "password": password}).encode()
    return await client.post("/login", content=body, headers=JSON)


@pytest.mark.parametrize("value, typed", VALUES)
async def test_a_seeded_value_signs_in_at_local_login(client, monkeypatch, value, typed):
    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    await _seed(value)
    response = await _login(client, typed)
    assert response.status_code == 200, response.text
    assert response.json()["token_type"] == "bearer"


@pytest.mark.parametrize("value, typed", VALUES)
async def test_a_seeded_value_signs_in_at_the_break_glass_login(client, monkeypatch, value, typed):
    monkeypatch.setattr(settings, "auth_method", "oidc", raising=False)
    monkeypatch.setattr(settings, "oidc_break_glass_local_superadmin", True, raising=False)
    await _seed(value)
    response = await _login(client, typed)
    assert response.status_code == 200, response.text


async def test_the_untrimmed_value_is_not_the_password(client, monkeypatch):
    """IAM-BOOT-3: the seed trims the value, so the password is the trimmed
    value; the configured value with its surrounding white space is a wrong
    password (401), not a second way in."""
    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    configured = " " + "zq" * 4 + " "
    await _seed(configured)
    assert (await _login(client, configured)).status_code == 401
    assert (await _login(client, "zq" * 4)).status_code == 200
