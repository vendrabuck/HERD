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


# -- The unusable-file ERROR is about the stored side only ----------------------------

# Raw JSON bodies, so an escape such as a lone surrogate reaches the route as the
# client wrote it. Every one is a string the route accepts.
_HOSTILE_STRING_BODIES = {
    "lone_surrogate": b'{"password": "\\ud800"}',
    "lone_low_surrogate": b'{"password": "ab\\udfffcd"}',
    "surrogate_after_byte_72": b'{"password": "' + b"a" * 80 + b'\\ud800"}',
    "nul": b'{"password": "\\u0000"}',
    "nul_inside": b'{"password": "test-config\\u0000pass"}',
    "empty": b'{"password": ""}',
    "over_long": b'{"password": "' + b"q" * 100_000 + b'"}',
    "bcrypt_hash_as_password": (
        b'{"password": "$2b$04$abcdefghijklmnopqrstuuabcdefghijklmnopqrstuvwxyz01234"}'
    ),
}
# Bodies the schema refuses before the password is checked.
_HOSTILE_NON_STRING_BODIES = {
    "null": b'{"password": null}',
    "number": b'{"password": 12345}',
    "list": b'{"password": ["test-config-pass"]}',
    "object": b'{"password": {"a": 1}}',
}


def _unusable_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if config_store.UNUSABLE_HASH_MESSAGE in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _HOSTILE_STRING_BODIES.values(), ids=_HOSTILE_STRING_BODIES.keys())
async def test_a_hostile_client_password_against_a_good_auth_file_never_blames_the_file(
    body, async_client, caplog
):
    """A 401, and no record naming the auth file: the operator's runbook for that
    line is to delete the file, so the client must not be able to produce it."""
    config_store.load_auth()  # the good file, seeded from CONFIG_ADMIN_PASSWORD
    with caplog.at_level(logging.DEBUG):
        resp = await async_client.post(
            "/login", content=body, headers={"content-type": "application/json"}
        )
    assert (resp.status_code, resp.json()) == (401, {"detail": "Invalid password"})
    assert _unusable_records(caplog) == []
    assert verify_password("test-config-pass") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", _HOSTILE_NON_STRING_BODIES.values(), ids=_HOSTILE_NON_STRING_BODIES.keys()
)
async def test_a_non_string_client_password_is_refused_by_the_schema_without_blaming_the_file(
    body, async_client, caplog
):
    config_store.load_auth()
    with caplog.at_level(logging.DEBUG):
        resp = await async_client.post(
            "/login", content=body, headers={"content-type": "application/json"}
        )
    assert resp.status_code == 422
    assert _unusable_records(caplog) == []


_HOSTILE_PASSWORDS = {
    "lone_surrogate": "\ud800",
    "surrogate_after_byte_72": "a" * 80 + "\ud800",
    "nul": "\x00",
    "none": None,
    "bytes": b"test-config-pass",
    "integer": 7,
}


@pytest.mark.parametrize("password", _HOSTILE_PASSWORDS.values(), ids=_HOSTILE_PASSWORDS.keys())
def test_check_password_logs_nothing_for_a_hostile_password_and_a_good_hash(password, caplog):
    stored = bcrypt.hashpw(b"test-config-pass", bcrypt.gensalt(4)).decode()
    with caplog.at_level(logging.DEBUG):
        assert config_store._check_password(password, stored) is False
    assert _unusable_records(caplog) == []


_UNUSABLE_STORED = {
    "none": None,
    "integer": 7,
    "empty": "",
    "garbage": "garbage",
    "cost_03": "$2b$03$" + "a" * 53,
    "lone_surrogate_in_file": "$2b$04$\ud800" + "a" * 52,
}


@pytest.mark.parametrize("stored", _UNUSABLE_STORED.values(), ids=_UNUSABLE_STORED.keys())
@pytest.mark.parametrize(
    "password",
    ["test-config-pass", *_HOSTILE_PASSWORDS.values()],
    ids=["good", *_HOSTILE_PASSWORDS.keys()],
)
def test_an_unusable_stored_hash_is_reported_exactly_once_whatever_the_password(
    stored, password, caplog
):
    """The other half of the iff: the stored side still reports itself, once,
    also when the client's password is hostile too."""
    with caplog.at_level(logging.DEBUG):
        assert config_store._check_password(password, stored) is False
    records = _unusable_records(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    if isinstance(stored, str) and stored:
        assert stored not in records[0].getMessage()


def test_a_change_password_that_cannot_be_encoded_keeps_the_file_and_blames_nothing(caplog):
    """change-password never reaches the check, so it cannot log the unusable-file
    line; a new password that is not UTF-8 encodable is refused (the route's 500
    is pre-existing) and the stored hash is left as it was."""
    config_store.load_auth()
    with caplog.at_level(logging.DEBUG), pytest.raises(UnicodeEncodeError):
        change_password("new-pass-\ud800-x")
    assert _unusable_records(caplog) == []
    assert verify_password("test-config-pass") is True


@pytest.mark.parametrize("password", _HOSTILE_PASSWORDS.values(), ids=_HOSTILE_PASSWORDS.keys())
def test_a_hostile_password_never_matches_a_hash_of_the_empty_password(password, caplog):
    """A refused password is checked against the stored hash with an empty
    placeholder secret, only to report a broken file; the answer stays False
    even when the stored hash is the hash of the empty password."""
    stored = bcrypt.hashpw(b"", bcrypt.gensalt(4)).decode()
    assert config_store._check_password("", stored) is True
    with caplog.at_level(logging.DEBUG):
        assert config_store._check_password(password, stored) is False
    assert _unusable_records(caplog) == []
