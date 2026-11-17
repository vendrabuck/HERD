"""The config-page password on bcrypt 5 (issue #1355).

Before #1355 `config_store` called bcrypt 4.0.1 directly, which used only the first
72 bytes of a password and raised ValueError for a stored value it could not read.
bcrypt 5 raises on more than 72 bytes, so the cut is now explicit.
`tests/data/bcrypt_4_0_1_fixtures.json` holds 284 decisions the old code made, run
under bcrypt 4.0.1 on the pre-#1355 commit (see its generator); the check here
accepts exactly the pairs it accepted and answers False, never an exception, for
every other pair, except that since #1429 a password holding a NUL, or an empty
one, never signs in (13 named pairs below).
"""

from __future__ import annotations

import itertools
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


# The accepted legacy pairs whose client password is now refused (#1429): every one
# sends an empty password or one holding a NUL. A change to this list is a change to
# which stored passwords keep signing in.
_LEGACY_ACCEPTS_NOW_REFUSED = [
    "cross:empty vs empty",
    "cross:nul_inside vs nul_inside",
    "mutation:ascii_72/nul_appended",
    "mutation:ascii_73/nul_appended",
    "mutation:emoji_32/nul_appended",
    "mutation:empty/first_72_bytes",
    "mutation:empty/nul_appended",
    "mutation:empty/self",
    "mutation:nul_inside/first_72_bytes",
    "mutation:nul_inside/self",
    "mutation:split_boundary/nul_appended",
    "production:empty/self",
    "production:nul_inside/self",
]


def test_every_legacy_decision_is_kept():
    """Accepted pairs stay accepted unless password_problem refuses the password;
    refused or raising pairs are False."""
    disagreements, now_refused = [], []
    for case in LEGACY_CASES:
        password = LEGACY_PASSWORDS[case["password_key"]]
        name = f"{case['group']}:{case['label']}"
        legal = config_store.password_problem(password) is None
        if case["old"] == "accept" and not legal:
            now_refused.append(name)
        expected = case["old"] == "accept" and legal
        if config_store._check_password(password, case["hash"]) is not expected:
            disagreements.append(f"{name} (old {case['old']})")
    assert disagreements == []
    assert sorted(now_refused) == _LEGACY_ACCEPTS_NOW_REFUSED


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
    "\U0001f511" * 18 + "\x00",
    "pw\x00" + "q" * 80,
    "pw",
    "ab\x00ab\x00ab",
    "ab",
    "",
    "\x00",
    "\x00" * 8,
    "\x01" * 8,
    "pw\ud800",
]


def test_two_passwords_match_exactly_when_their_first_72_utf8_bytes_match(monkeypatch):
    """The invariant over every hash the service can store: a password signs in
    exactly when it is legal and its first 72 UTF-8 bytes equal the stored one's.
    No NUL trick can make two different keys collide, because no stored password
    and no signing-in password holds a NUL; an illegal password is never hashed."""
    monkeypatch.setattr(config_store, "BCRYPT_ROUNDS", 4)
    stored_count = 0
    for p in _BOUNDARY:
        if config_store.password_problem(p) is not None:
            with pytest.raises(config_store.InvalidPassword):
                config_store._hash_password(p)
            continue
        stored = config_store._hash_password(p)
        stored_count += 1
        for q in _BOUNDARY:
            same = (
                config_store.password_problem(q) is None
                and p.encode()[:72] == q.encode("utf-8", "surrogatepass")[:72]
            )
            assert config_store._check_password(q, stored) is same, (p[:3], len(p), len(q))
    assert stored_count == 14


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
    "all_nul": "\x00" * 8,
    "nul_separated": "ab\x00ab\x00ab",
    "empty": "",
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


@pytest.mark.parametrize(
    ("password", "detail"),
    [
        ("new-pass-\ud800-x", "Password must be valid UTF-8 text"),
        ("new\x00pass-x", "Password must not contain a NUL character"),
        ("short", "Password must be at least 8 characters"),
    ],
    ids=["lone_surrogate", "nul", "short"],
)
def test_a_change_password_that_is_refused_keeps_the_file_and_blames_nothing(
    password, detail, tmp_config_dir, caplog
):
    """change-password never reaches the check, so it cannot log the unusable-file
    line; a refused new password raises InvalidPassword with a fixed message (the
    route answers it as a 422, issue #1428) and the file is left as it was."""
    config_store.load_auth()
    path = Path(tmp_config_dir) / "config_auth.json"
    before = path.read_bytes()
    with caplog.at_level(logging.DEBUG), pytest.raises(config_store.InvalidPassword) as err:
        change_password(password)
    assert str(err.value) == detail
    assert path.read_bytes() == before
    assert _unusable_records(caplog) == []
    assert verify_password("test-config-pass") is True


# The passwords the check refuses outright: every non-string, and every string
# password_problem refuses (a NUL, a lone surrogate, empty). bcrypt itself makes
# "\x00" match a hash of "" (the old bcrypt 4.0.1 code accepted that pair too, see
# the legacy table), so the refusal cannot be left to bcrypt (#1429).
_REFUSED_PASSWORDS = {
    k: v
    for k, v in _HOSTILE_PASSWORDS.items()
    if not isinstance(v, str) or config_store.password_problem(v) is not None
}


def test_the_refused_set_is_the_non_strings_the_non_encodable_nul_and_empty():
    assert sorted(_REFUSED_PASSWORDS) == [
        "all_nul",
        "bytes",
        "empty",
        "integer",
        "lone_surrogate",
        "none",
        "nul",
        "nul_separated",
        "surrogate_after_byte_72",
    ]


@pytest.mark.parametrize("password", _REFUSED_PASSWORDS.values(), ids=_REFUSED_PASSWORDS.keys())
@pytest.mark.parametrize(
    "stored_secret", [b"", b"\x00" * 8, b"ab\x00ab\x00ab"], ids=["empty", "all_nul", "nul_sep"]
)
def test_a_hostile_password_never_matches_a_hash_of_the_empty_password(
    password, stored_secret, caplog
):
    """A refused password is checked against the stored hash with an empty
    placeholder secret, only to report a broken file; the answer stays False even
    when the stored hash is one bcrypt would match it to: a hash of the empty
    password, or one stored before #1429 from NULs."""
    stored = bcrypt.hashpw(stored_secret, bcrypt.gensalt(4)).decode()
    with caplog.at_level(logging.DEBUG):
        assert config_store._check_password(password, stored) is False
    assert _unusable_records(caplog) == []


# -- The legal config password at every entry point (issues #1428 and #1429) ----------
#
# bcrypt appends a NUL to the key and repeats it to fill 72 bytes, so a password made
# only of NULs has the key of the empty password, and NUL separators can make a
# password equal a shorter one. A lone surrogate cannot be encoded as UTF-8 at all.
# Neither may become the config password, and neither ever signs in.

NUL_DETAIL = "Password must not contain a NUL character"
NOT_UTF8_DETAIL = "Password must be valid UTF-8 text"
TOO_SHORT_DETAIL = "Password must be at least 8 characters"
TOO_LONG_DETAIL = "Password must be at most 32 characters"


def _json_body(field: str, password: str) -> bytes:
    """A raw JSON body with every non-ASCII code point escaped, so a lone surrogate
    or a NUL reaches the route exactly as the client wrote it (`json=` cannot send
    a lone surrogate)."""
    return json.dumps({field: password}, ensure_ascii=True).encode("ascii")


# New passwords of 8 to 32 characters the change-password route refuses, with the
# detail each one gets: the NUL rule is checked before the UTF-8 rule.
_REFUSED_NEW_PASSWORDS = {
    "all_nul_8": ("\x00" * 8, NUL_DETAIL),
    "all_nul_32": ("\x00" * 32, NUL_DETAIL),
    "nul_first": ("\x00abcdefg", NUL_DETAIL),
    "nul_inside": ("abcd\x00efg", NUL_DETAIL),
    "nul_last": ("abcdefg\x00", NUL_DETAIL),
    "nul_separators": ("ab\x00ab\x00ab", NUL_DETAIL),
    "nul_past_byte_72": ("\U0001f511" * 18 + "\x00", NUL_DETAIL),
    "lone_high_surrogate": ("abcdefg\ud800", NOT_UTF8_DETAIL),
    "lone_low_surrogate": ("\udfffabcdefg", NOT_UTF8_DETAIL),
    "reversed_pair": ("abcdef\udd11\ud83d", NOT_UTF8_DETAIL),
    "undecodable_byte": ("abcdefg\udcff", NOT_UTF8_DETAIL),
    "surrogate_past_byte_72": ("\U0001f511" * 18 + "\ud800", NOT_UTF8_DETAIL),
    "nul_and_surrogate": ("\x00bcdefg\ud800", NUL_DETAIL),
}
# The length bound is checked first and keeps its wording.
_REFUSED_BY_LENGTH = {
    "empty": ("", TOO_SHORT_DETAIL),
    "seven": ("a" * 7, TOO_SHORT_DETAIL),
    "one_nul": ("\x00", TOO_SHORT_DETAIL),
    "short_surrogate": ("abc\ud800", TOO_SHORT_DETAIL),
    "thirty_three": ("a" * 33, TOO_LONG_DETAIL),
    "thirty_three_nul": ("\x00" * 33, TOO_LONG_DETAIL),
}
# New passwords that must keep working: every other control character, non-ASCII
# text, a character outside the BMP sent as an escaped surrogate PAIR (one code point
# once parsed), and both ends of the length bound.
_ACCEPTED_NEW_PASSWORDS = {
    "eight": "a" * 8,
    "thirty_two": "b" * 32,
    "non_ascii": "p\u00e4ssw\u00f6rd-\u65e5\u672c",
    "c0_controls": "\x01\x02\x03\x04\x05\x06\x07\x08",
    "tab_and_newline": "tab\tnew\nline",
    "delete": "\x7f" * 8,
    "c1_and_separators": "\x85\u2028\u2029abcde",
    "noncharacters": "\ufffe\uffffabcdef",
    "surrogate_pair_escape": "\U0001f600" * 8,
    "spaces": " " * 8,
}


async def _session(client) -> dict:
    resp = await client.post("/login", json={"password": "test-config-pass"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _post_raw(client, path: str, body: bytes, headers: dict | None = None):
    return await client.post(
        path, content=body, headers={"content-type": "application/json", **(headers or {})}
    )


def _assert_not_echoed(resp, password: str) -> None:
    if not password:
        return  # nothing to echo
    assert _json_body("x", password)[7:-2] not in resp.content
    assert password.encode("utf-8", "surrogatepass") not in resp.content


@pytest.fixture
def login_never_waits(monkeypatch):
    """A limiter whose clock jumps a day per read, so a test that sends several
    wrong passwords on purpose never meets a wait (the limits are not the subject)."""
    from app import login_limits

    ticks = itertools.count(0, 86_400)
    monkeypatch.setattr(
        login_limits, "LIMITER", login_limits.LoginLimiter(clock=lambda: float(next(ticks)))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("password", "detail"),
    [*_REFUSED_NEW_PASSWORDS.values(), *_REFUSED_BY_LENGTH.values()],
    ids=[*_REFUSED_NEW_PASSWORDS, *_REFUSED_BY_LENGTH],
)
async def test_change_password_refuses_an_illegal_password_without_echo_and_keeps_the_file(
    password, detail, tmp_config_dir, async_client, caplog
):
    headers = await _session(async_client)
    path = Path(tmp_config_dir) / "config_auth.json"
    before = path.read_bytes()
    with caplog.at_level(logging.DEBUG):
        resp = await _post_raw(
            async_client, "/change-password", _json_body("new_password", password), headers
        )
    assert (resp.status_code, resp.json()) == (422, {"detail": detail})
    _assert_not_echoed(resp, password)
    assert path.read_bytes() == before
    assert _unusable_records(caplog) == []
    login = await async_client.post("/login", json={"password": "test-config-pass"})
    assert login.status_code == 200
    assert login.json()["password_changed"] is True
    empty = await async_client.post("/login", json={"password": ""})
    assert empty.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "password", _ACCEPTED_NEW_PASSWORDS.values(), ids=_ACCEPTED_NEW_PASSWORDS.keys()
)
async def test_change_password_accepts_every_other_password_and_it_signs_in(password, async_client):
    headers = await _session(async_client)
    resp = await _post_raw(
        async_client, "/change-password", _json_body("new_password", password), headers
    )
    assert (resp.status_code, resp.json()) == (200, {"message": "Password changed"})
    login = await _post_raw(async_client, "/login", _json_body("password", password))
    assert login.status_code == 200
    old = await async_client.post("/login", json={"password": "test-config-pass"})
    assert old.status_code == 401


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_never_waits")
async def test_eight_nuls_never_become_the_password_and_the_empty_password_never_signs_in(
    async_client,
):
    """The issue #1429 sequence: the change is refused, and afterwards neither the
    empty password nor any run of NULs signs in; the old password still does."""
    headers = await _session(async_client)
    resp = await _post_raw(
        async_client, "/change-password", _json_body("new_password", "\x00" * 8), headers
    )
    assert (resp.status_code, resp.json()) == (422, {"detail": NUL_DETAIL})
    for attempt in ["", "\x00", "\x00" * 3, "\x00" * 8, "\x00xyz"]:
        login = await _post_raw(async_client, "/login", _json_body("password", attempt))
        assert (login.status_code, login.json()) == (401, {"detail": "Invalid password"})
    assert (
        await async_client.post("/login", json={"password": "test-config-pass"})
    ).status_code == 200


# A stored hash written before #1429 by a change to a password holding NULs: what
# each login attempt gets now. The NUL-only hash has the key of the empty password.
_LEGACY_NUL_STORED = {
    "eight_nuls": "\x00" * 8,
    "nul_separators": "ab\x00ab\x00ab",
    "nul_inside": "abc\x00defgh",
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_never_waits")
@pytest.mark.parametrize("stored", _LEGACY_NUL_STORED.values(), ids=_LEGACY_NUL_STORED.keys())
async def test_a_legacy_hash_of_a_password_with_a_nul_never_signs_in_with_a_nul_or_empty_password(
    stored, tmp_config_dir, async_client, caplog
):
    """The empty password and every password holding a NUL, the legacy password
    itself included, are 401 with no unusable-file line: the operator who set such a
    password removes the auth file to recover (OPS-CONFIG-7)."""
    hashed = bcrypt.hashpw(stored.encode(), bcrypt.gensalt(4)).decode()
    _write_auth(tmp_config_dir, {"password_hash": hashed, "password_changed": True})
    # The premise: bcrypt itself matches the legacy password, and for the NUL-only
    # hash also the empty password and any run of NULs.
    assert bcrypt.checkpw(stored.encode(), hashed.encode()) is True
    assert bcrypt.checkpw(b"", hashed.encode()) is (set(stored) == {"\x00"})
    with caplog.at_level(logging.DEBUG):
        for attempt in ["", "\x00", "\x00" * 8, "\x00" * 100, stored, stored + "\x00"]:
            login = await _post_raw(async_client, "/login", _json_body("password", attempt))
            assert (login.status_code, login.json()) == (401, {"detail": "Invalid password"})
    assert _unusable_records(caplog) == []


@pytest.mark.asyncio
async def test_a_legacy_nul_separated_hash_still_matches_its_shorter_twin(
    tmp_config_dir, async_client
):
    """The one door the login side cannot close: a NUL-free password whose bcrypt key
    equals the legacy one ("ab" against "ab\\x00ab\\x00ab") still signs in. Only the
    operator can close it, by changing the password or removing the file."""
    hashed = bcrypt.hashpw(b"ab\x00ab\x00ab", bcrypt.gensalt(4)).decode()
    _write_auth(tmp_config_dir, {"password_hash": hashed, "password_changed": True})
    assert (await async_client.post("/login", json={"password": "ab"})).status_code == 200


# CONFIG_ADMIN_PASSWORD is the other way a password enters the service. The process
# environment cannot hold a NUL, but a byte that is not UTF-8 decodes to a lone
# surrogate (Python's surrogateescape), which cannot be hashed.

ENV_REFUSED_LOG = (
    "CONFIG_ADMIN_PASSWORD is not a usable config password (Password must be valid UTF-8 "
    "text); no auth file written, login refused"
)


def test_the_environment_cannot_hold_a_nul(monkeypatch):
    with pytest.raises(ValueError, match="embedded null byte"):
        monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", "env\x00pass")


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_never_waits")
async def test_an_admin_password_that_is_not_utf8_seeds_nothing_and_fails_closed(
    monkeypatch, tmp_config_dir, async_client, caplog
):
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", "envpass\udcff")
    assert os.environ["CONFIG_ADMIN_PASSWORD"] == "envpass\udcff"
    with caplog.at_level(logging.DEBUG):
        status = await async_client.get("/status")
        login = await async_client.post("/login", json={"password": "envpass"})
        login_raw = await _post_raw(async_client, "/login", _json_body("password", "envpass\udcff"))
    assert (status.status_code, status.json()) == (
        200,
        {"configured": False, "password_changed": False},
    )
    assert (login.status_code, login.json()) == (401, {"detail": "Invalid password"})
    assert (login_raw.status_code, login_raw.json()) == (401, {"detail": "Invalid password"})
    assert not (Path(tmp_config_dir) / "config_auth.json").exists()
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert set(messages) == {ENV_REFUSED_LOG}
    assert all("envpass" not in m for m in messages)
    # Fixing the variable is the recovery: nothing was stored in the meantime.
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", "envpass-fixed")
    assert verify_password("envpass-fixed") is True


@pytest.mark.parametrize("value", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_a_blank_admin_password_still_means_a_generated_one(value, monkeypatch, caplog):
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", value)
    monkeypatch.setattr(config_store.secrets, "token_urlsafe", lambda n=24: "generated-pw-1234")
    assert verify_password("generated-pw-1234") is True
    assert verify_password(value) is False
    assert config_store.is_password_changed() is False


@pytest.mark.parametrize(
    "value",
    ["env\tpass\x01word", "p\u00e4ss-\u65e5\u672c", "a", "\U0001f511" * 40],
    ids=["controls", "non_ascii", "one_char", "over_72_bytes"],
)
def test_a_legal_admin_password_of_any_length_seeds_and_signs_in(value, monkeypatch):
    """The environment has no length bound (only change-password has one)."""
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", value)
    assert verify_password(value) is True
    assert config_store.is_password_changed() is True


# -- The helper itself ------------------------------------------------------------

_PASSWORD_SPACE = {
    # legal, any length: the length bound is change-password's alone
    "one_char": ("a", None),
    "eight": ("a" * 8, None),
    "long_ascii": ("q" * 10_000, None),
    "non_ascii": ("p\u00e4ssw\u00f6rd-\u65e5\u672c", None),
    "astral": ("\U0001f511" * 40, None),
    "c0_controls": ("".join(chr(c) for c in range(1, 32)), None),
    "delete_and_c1": ("\x7f\x80\x85\x9f", None),
    "separators": ("\u2028\u2029", None),
    "noncharacters": ("\ufffe\uffff", None),
    "bom": ("\ufeffabc", None),
    "whitespace_only": (" \t\n", None),
    # NUL anywhere
    "nul_only": ("\x00", PASSWORD_NUL := "Password must not contain a NUL character"),
    "nul_run": ("\x00" * 100, PASSWORD_NUL),
    "nul_first": ("\x00abc", PASSWORD_NUL),
    "nul_inside": ("ab\x00cd", PASSWORD_NUL),
    "nul_last": ("abcd\x00", PASSWORD_NUL),
    "nul_past_byte_72": ("q" * 100 + "\x00", PASSWORD_NUL),
    # not UTF-8 encodable
    "lone_high": ("\ud800", PASSWORD_UTF8 := "Password must be valid UTF-8 text"),
    "lone_low": ("\udfff", PASSWORD_UTF8),
    "highest_high": ("abc\udbff", PASSWORD_UTF8),
    "lowest_low": ("\udc00abc", PASSWORD_UTF8),
    "reversed_pair": ("\udd11\ud83d", PASSWORD_UTF8),
    "unjoined_pair": ("\ud83d\udd11", PASSWORD_UTF8),
    "surrogateescape": ("env\udcff", PASSWORD_UTF8),
    "surrogate_past_byte_72": ("q" * 100 + "\ud800", PASSWORD_UTF8),
    # both: the NUL rule is checked first
    "nul_and_surrogate": ("\ud800\x00", PASSWORD_NUL),
    # empty
    "empty": ("", "Password must not be empty"),
}


@pytest.mark.parametrize(
    ("password", "problem"), _PASSWORD_SPACE.values(), ids=_PASSWORD_SPACE.keys()
)
def test_password_problem_enumerates_the_space(password, problem):
    assert config_store.password_problem(password) == problem


def test_every_legal_password_in_the_space_hashes_and_signs_in(monkeypatch):
    monkeypatch.setattr(config_store, "BCRYPT_ROUNDS", 4)
    for name, (password, problem) in _PASSWORD_SPACE.items():
        if problem is None:
            assert config_store._check_password(password, config_store._hash_password(password)), (
                name
            )


def test_every_refused_nul_or_empty_password_has_a_different_bcrypt_twin():
    """Why NUL and empty are refused: bcrypt's key is the bytes plus a NUL, repeated
    to 72 bytes, so s and s + NUL + s share a key, and every all-NUL password shares
    the empty one's. A NUL past byte 72 changes no key; it is refused because the
    rule is a NUL anywhere (issue #1429, as IAM-HASH-3 in the auth service)."""
    salt = bcrypt.gensalt(4)
    checked = []
    for name, (password, problem) in _PASSWORD_SPACE.items():
        if problem not in {
            "Password must not contain a NUL character",
            "Password must not be empty",
        }:
            continue
        try:
            secret = password.encode()[:72]
        except UnicodeEncodeError:
            continue  # nul_and_surrogate: cannot be hashed at all
        if b"\x00" not in secret and secret:
            assert name == "nul_past_byte_72"
            continue
        twin = (b"\x00" if not secret else b"") if set(secret) <= {0} else secret + b"\x00" + secret
        assert twin != secret
        assert bcrypt.checkpw(twin[:72], bcrypt.hashpw(secret, salt)), name
        checked.append(name)
    assert sorted(checked) == [
        "empty",
        "nul_first",
        "nul_inside",
        "nul_last",
        "nul_only",
        "nul_run",
    ]


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("", "Password must be at least 8 characters"),
        ("a" * 7, "Password must be at least 8 characters"),
        ("\x00" * 7, "Password must be at least 8 characters"),
        ("a" * 8, None),
        ("\U0001f511" * 32, None),
        ("a" * 32, None),
        ("a" * 33, "Password must be at most 32 characters"),
        ("\x00" * 33, "Password must be at most 32 characters"),
        ("\x00" * 8, "Password must not contain a NUL character"),
        ("\x00" * 32, "Password must not contain a NUL character"),
        ("abcdefg\ud800", "Password must be valid UTF-8 text"),
        ("\x01" * 8, None),
    ],
)
def test_new_password_problem_checks_the_bound_in_characters_then_the_rule(password, problem):
    assert config_store.new_password_problem(password) == problem


@pytest.mark.parametrize(
    "password", ["\x00" * 8, "ab\x00ab\x00ab", "abc\ud800", ""], ids=["nul", "nul_sep", "sur", "e"]
)
def test_an_illegal_password_never_reaches_bcrypt(password, monkeypatch):
    calls: list = []
    monkeypatch.setattr(bcrypt, "hashpw", lambda *a: calls.append(a))
    with pytest.raises(config_store.InvalidPassword) as err:
        config_store._hash_password(password)
    assert str(err.value) == config_store.password_problem(password)
    assert calls == []
