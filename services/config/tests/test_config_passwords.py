"""The config-page password on bcrypt 5 (issue #1355).

Before #1355 `config_store` called bcrypt 4.0.1 directly, which used only the first
72 bytes of a password and raised ValueError for a stored value it could not read.
bcrypt 5 raises on more than 72 bytes, so the cut is now explicit.
`tests/data/bcrypt_4_0_1_fixtures.json` holds 284 decisions the old code made, run
under bcrypt 4.0.1 on the pre-#1355 commit (see its generator); the check here
accepts exactly the pairs it accepted and answers False, never an exception, for
every other pair.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import bcrypt
import pytest
from app import config_store
from app.config_store import change_password, verify_password
from app.main import app
from httpx import ASGITransport, AsyncClient

DATA = Path(__file__).parent / "data"
LEGACY = json.loads((DATA / "bcrypt_4_0_1_fixtures.json").read_text())
LEGACY_PASSWORDS: dict[str, str] = LEGACY["passwords"]
LEGACY_CASES: list[dict] = LEGACY["cases"]


def _write_auth(tmp_config_dir: str, payload) -> None:
    with open(os.path.join(tmp_config_dir, "config_auth.json"), "w") as f:
        json.dump(payload, f)


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_the_legacy_table_came_from_the_old_check_on_bcrypt_4_0_1():
    provenance = LEGACY["provenance"]
    assert provenance["bcrypt"] == "4.0.1"
    assert provenance["old_check"] == "bcrypt.checkpw(password.encode(), hashed.encode())"
    assert len(LEGACY_CASES) == 284
    assert {c["group"] for c in LEGACY_CASES} == {
        "production",
        "cross",
        "mutation",
        "ident",
        "cost",
        "malformed",
        "padding",
    }


def test_every_legacy_decision_is_kept():
    """Accepted pairs stay accepted; refused or raising pairs are False."""
    disagreements = []
    for case in LEGACY_CASES:
        password = LEGACY_PASSWORDS[case["password_key"]]
        expected = case["old"] == "accept"
        if config_store._check_password(password, case["hash"]) is not expected:
            disagreements.append(f"{case['group']}:{case['label']} (old {case['old']})")
    assert disagreements == []


def test_new_hashes_are_2b_at_cost_12():
    stored = config_store._hash_password("config-page-pass")
    assert stored.startswith("$2b$12$") and len(stored) == 60
    assert config_store.BCRYPT_ROUNDS == 12


_BOUNDARY = [
    "A" * 70 + s for s in ["", "B", "BC", "BD", "\u00e9", "\u00e9X", "\u65e5", "\u65e6"]
] + [
    "\U0001f511" * 32,
    "\U0001f511" * 18,
    "\U0001f511" * 17,
    "pw\x00" + "q" * 80,
    "pw",
]


def test_two_passwords_match_exactly_when_their_first_72_utf8_bytes_match(monkeypatch):
    monkeypatch.setattr(config_store, "BCRYPT_ROUNDS", 4)
    for p in _BOUNDARY:
        stored = config_store._hash_password(p)
        for q in _BOUNDARY:
            same = p.encode()[:72] == q.encode()[:72]
            assert config_store._check_password(q, stored) is same, (p[:3], len(p), len(q))


def test_bcrypt_never_receives_more_than_72_bytes(monkeypatch):
    seen: list[bytes] = []
    real_hashpw, real_checkpw = bcrypt.hashpw, bcrypt.checkpw
    monkeypatch.setattr(bcrypt, "hashpw", lambda s, salt: seen.append(s) or real_hashpw(s, salt))
    monkeypatch.setattr(bcrypt, "checkpw", lambda s, h: seen.append(s) or real_checkpw(s, h))
    monkeypatch.setattr(config_store, "BCRYPT_ROUNDS", 4)
    password = "\U0001f511" * 2500  # 10000 bytes; the login body is unbounded
    stored = config_store._hash_password(password)
    assert config_store._check_password(password, stored) is True
    assert seen == [password.encode()[:72]] * 2


_UNUSABLE = {
    "missing_key": {"password_changed": True},
    "null_hash": {"password_hash": None, "password_changed": True},
    "integer_hash": {"password_hash": 7, "password_changed": True},
    "empty_hash": {"password_hash": "", "password_changed": True},
    "garbage_hash": {"password_hash": "garbage", "password_changed": True},
    "cost_03": {"password_hash": "$2b$03$" + "a" * 53, "password_changed": True},
    "not_an_object": ["password_hash"],
}


@pytest.mark.parametrize("payload", _UNUSABLE.values(), ids=_UNUSABLE.keys())
def test_an_unusable_auth_file_refuses_login_without_raising(payload, tmp_config_dir, caplog):
    _write_auth(tmp_config_dir, payload)
    with caplog.at_level(logging.ERROR):
        assert verify_password("test-config-pass") is False
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(config_store.UNUSABLE_HASH_MESSAGE) for m in messages), messages
    for message in messages:
        assert "test-config-pass" not in message and "garbage" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", _UNUSABLE.values(), ids=_UNUSABLE.keys())
async def test_login_against_an_unusable_auth_file_is_401_not_500(
    payload, tmp_config_dir, async_client
):
    _write_auth(tmp_config_dir, payload)
    resp = await async_client.post("/login", json={"password": "test-config-pass"})
    assert (resp.status_code, resp.json()) == (401, {"detail": "Invalid password"})


@pytest.mark.asyncio
async def test_a_stored_legacy_hash_over_72_bytes_still_signs_in(tmp_config_dir, async_client):
    """A config password the old code stored (32 key emoji, 128 bytes, the most
    the change-password route allows) signs in with the same password and with any
    password sharing its first 72 bytes, as before."""
    (case,) = [
        c for c in LEGACY_CASES if (c["group"], c["label"]) == ("production", "emoji_32/self")
    ]
    _write_auth(tmp_config_dir, {"password_hash": case["hash"], "password_changed": True})
    password = LEGACY_PASSWORDS[case["password_key"]]
    for attempt, status in [(password, 200), ("\U0001f511" * 18, 200), ("\U0001f511" * 17, 401)]:
        resp = await async_client.post("/login", json={"password": attempt})
        assert resp.status_code == status


@pytest.mark.asyncio
async def test_change_password_to_32_multibyte_characters_then_sign_in(async_client):
    login = await async_client.post("/login", json={"password": "test-config-pass"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    new = "\U0001f511" * 32
    resp = await async_client.post("/change-password", json={"new_password": new}, headers=headers)
    assert resp.status_code == 200
    assert (await async_client.post("/login", json={"password": new})).status_code == 200
    assert verify_password("\U0001f511" * 18) is True
    assert verify_password("\U0001f511" * 17) is False


def test_an_operator_password_over_72_bytes_seeds_and_verifies(monkeypatch):
    long_password = "operator-" + "x" * 200
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", long_password)
    assert verify_password(long_password) is True
    assert verify_password(long_password[:72]) is True
    assert verify_password(long_password[:71]) is False
    change_password("short-pass-1")
    assert verify_password(long_password) is False
