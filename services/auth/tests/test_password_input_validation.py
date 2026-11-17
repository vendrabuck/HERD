"""Which strings are a legal password, at every auth entry point (issue #1426).

The rule lives once, in `app/services/passwords.py` (`validate_password`): a
password is refused when it holds a NUL character, is longer than 4096 UTF-8 bytes,
or cannot be encoded as UTF-8 (a lone surrogate). `hash_password` applies it, so
registration and the superadmin seed can never store a password bcrypt was not
given, and `RegisterRequest` applies the same function as a field validator, so a
refused password is a 422 at `POST /register` instead of a 500 from the hashing
step. The pre-check is pinned EQUAL to the hashing step over a corpus of passwords,
never stricter. `LoginRequest` is deliberately unchanged: a NUL at login stays the
401 of IAM-HASH-3, and a lone surrogate is refused by pydantic's own `str` check.

Every 422 here also passes through auth's validation handler
(`app/validation_errors.py`): the response never echoes a submitted value. Raw
bodies are sent with `content=`, because `json=` cannot encode a lone surrogate.
"""

from __future__ import annotations

import json

import pytest
from app.models.user import Role, User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services import passwords
from pydantic import ValidationError
from sqlalchemy import func, select

from tests._harness import TestSessionLocal, mock_user

NUL_MESSAGE = "Password must not contain a NUL character"
JSON = {"content-type": "application/json"}
INVALID = {"detail": "Invalid credentials"}


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


def _register_body(password_json: str, username_json: str = '"newuser"') -> bytes:
    """A raw register body; the password is spliced in as JSON text, escapes and all."""
    return (
        '{"email": "new@example.com", "username": '
        + username_json
        + ', "password": '
        + password_json
        + "}"
    ).encode()


# -- 1. The one rule, at its source ---------------------------------------------------


def test_the_nul_message_is_pinned():
    assert passwords.PASSWORD_NUL_MESSAGE == NUL_MESSAGE


@pytest.mark.parametrize(
    "password",
    ["\x00abcdefgh", "abcd\x00efgh", "abcdefgh\x00", "a" * 80 + "\x00"],
    ids=["first", "middle", "last", "past_byte_72"],
)
def test_validate_password_refuses_a_nul_anywhere_with_the_pinned_message(password):
    with pytest.raises(ValueError) as caught:
        passwords.validate_password(password)
    assert str(caught.value) == NUL_MESSAGE


@pytest.mark.parametrize(
    "password, fragment",
    [
        ("abcdefg\ud800", "UTF-8"),
        ("\u00e9" * 2048 + "a", "4096"),
        (b"bytes-password", "string"),
    ],
    ids=["lone_surrogate", "4097_bytes", "not_a_string"],
)
def test_validate_password_refuses_the_rest_with_a_message_that_holds_no_input(password, fragment):
    with pytest.raises(ValueError) as caught:
        passwords.validate_password(password)
    message = str(caught.value)
    assert fragment in message
    assert "abcdefg" not in message and "\ud800" not in message and "bytes-password" not in message
    # No chained exception either: UnicodeEncodeError's own text quotes the character.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_validate_password_returns_an_honest_password_unchanged():
    for password in ("abcdefgh", "\U0001f511" * 72, "\u00e9" * 2048, " spaced  out "):
        assert passwords.validate_password(password) is password


# A corpus around every refusal; each entry 8 to 72 characters, so only the
# hashability rule decides it (the length bound is IAM-REG-2, checked elsewhere).
_CORPUS = [
    "abcdefgh",
    "a" * 72,
    "\U0001f511" * 72,  # 288 UTF-8 bytes: over 72 bytes, under 4096
    "\u00e9" * 72,
    "tab\there!",
    "newline\nok",
    "\x01control",
    "\x7fdelete!",
    "nul\x00inside",
    "\x00leading",
    "trailing\x00",
    "lone\ud800high",
    "lone\udfffloww",
    "\udc80escaped",  # what surrogateescape makes of a non-UTF-8 byte
    "\ufffdreplace",
    "\U0010ffffmaxcode",
]


@pytest.mark.parametrize("password", _CORPUS, ids=[repr(p) for p in _CORPUS])
def test_register_accepts_a_password_exactly_when_hashing_does(password, monkeypatch):
    """The pre-check equals the judge: never stricter, never looser."""
    monkeypatch.setattr(passwords, "BCRYPT_ROUNDS", 4)
    try:
        passwords.hash_password(password)
        hashable = True
    except ValueError:
        hashable = False
    try:
        RegisterRequest(email="a@example.com", username="abc", password=password)
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted is hashable


def test_register_schema_names_the_password_and_the_pinned_message():
    with pytest.raises(ValidationError) as caught:
        RegisterRequest(email="a@example.com", username="abc", password="abcdefg\x00")
    (error,) = caught.value.errors()
    assert (error["type"], error["loc"], error["msg"]) == (
        "value_error",
        ("password",),
        "Value error, " + NUL_MESSAGE,
    )


def test_login_schema_still_takes_a_nul_so_login_answers_401():
    """IAM-HASH-3: the login refusal of a NUL is the 401, decided by verify_password."""
    assert LoginRequest(email="a@example.com", password="abc\x00def").password == "abc\x00def"


# -- 2. Through the real routes -------------------------------------------------------


async def test_register_with_a_nul_is_422_with_the_pinned_wording_and_creates_nothing(client):
    response = await client.post(
        "/register", content=_register_body('"abcdefg\\u0000"'), headers=JSON
    )
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "password"],
                "msg": "Value error, " + NUL_MESSAGE,
            }
        ]
    }
    assert "abcdefg" not in response.text
    assert await _user_count() == 0


@pytest.mark.parametrize("escape", ["\\ud800", "\\udfff", "\\udc80"])
async def test_register_with_a_lone_surrogate_is_422_string_unicode_and_creates_nothing(
    client, escape
):
    response = await client.post(
        "/register", content=_register_body(f'"abcdefg{escape}"'), headers=JSON
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("string_unicode", ["body", "password"])
    assert "abcdefg" not in response.text
    assert await _user_count() == 0


async def test_login_with_a_lone_surrogate_is_422_string_unicode(client):
    response = await client.post(
        "/login",
        content=b'{"email": "a@example.com", "password": "abcdefg\\ud800"}',
        headers=JSON,
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("string_unicode", ["body", "password"])
    assert "abcdefg" not in response.text


async def test_login_with_a_nul_stays_401(client):
    for email in ("a@example.com", "nobody@example.com"):
        response = await client.post(
            "/login",
            content=json.dumps({"email": email, "password": "abc\x00defgh"}).encode(),
            headers=JSON,
        )
        assert (response.status_code, response.json()) == (401, INVALID)


async def test_a_surrogate_in_another_register_field_is_422_not_500(client):
    response = await client.post(
        "/register",
        content=_register_body('"honest-password"', username_json='"ab\\ud800c"'),
        headers=JSON,
    )
    assert response.status_code == 422, response.text
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "username"]]
    assert await _user_count() == 0


async def test_a_register_with_an_honest_8_character_password_still_succeeds(client):
    response = await client.post(
        "/register",
        content=_register_body('"abcdefgh"'),
        headers=JSON,
    )
    assert response.status_code == 201, response.text
    assert await _user_count() == 1


# -- 3. No 422 holds a submitted password ---------------------------------------------
#
# The guarantee (IAM-HASH-9) is about passwords: no 422 carries pydantic's `input`
# or `ctx`, and every error a password field can raise has fixed text. The matrix
# below drives every way a password-bearing body can fail, for each schema that
# carries one, and checks the password appears nowhere in the response. A failure
# that is not itself a 422 (an extra key, a NUL at login) is paired with a broken
# sibling so the body still fails validation with the password in it.

_PW = "Pw-Secret-7731"
_SHORT_PW = "Pw-S3c!"  # 7 characters, under the register minimum

_REGISTER_OK = {"email": "e@example.com", "username": "abc"}
_LOGIN_OK = {"email": "e@example.com"}

_PASSWORD_FAILURES = {
    # register
    "register-missing_sibling": ("/register", {"email": "e@example.com", "password": _PW}),
    "register-bad_sibling": ("/register", {**_REGISTER_OK, "username": "a b", "password": _PW}),
    "register-wrong_type_list": ("/register", {**_REGISTER_OK, "password": [_PW]}),
    "register-wrong_type_object": ("/register", {**_REGISTER_OK, "password": {_PW: _PW}}),
    "register-too_short": ("/register", {**_REGISTER_OK, "password": _SHORT_PW}),
    "register-too_long": ("/register", {**_REGISTER_OK, "password": _PW + "x" * 80}),
    "register-nul": ("/register", {**_REGISTER_OK, "password": _PW + "\x00"}),
    "register-surrogate": ("/register", {**_REGISTER_OK, "password": _PW + "\ud800"}),
    "register-extra_key": (
        "/register",
        {"email": "e@example.com", "password": _PW, "password_confirm": _PW},
    ),
    # login
    "login-missing_sibling": ("/login", {"password": _PW}),
    "login-empty_sibling": ("/login", {"email": "", "password": _PW}),
    "login-wrong_type_list": ("/login", {**_LOGIN_OK, "password": [_PW]}),
    "login-wrong_type_object": ("/login", {**_LOGIN_OK, "password": {_PW: _PW}}),
    "login-too_long": ("/login", {**_LOGIN_OK, "password": _PW + "x" * 80}),
    "login-nul": ("/login", {"password": _PW + "\x00"}),  # alone a NUL is the 401
    "login-surrogate": ("/login", {**_LOGIN_OK, "password": _PW + "\ud800"}),
    "login-extra_key": ("/login", {"password": _PW, "password_confirm": _PW}),
}


@pytest.mark.parametrize("case", _PASSWORD_FAILURES.values(), ids=_PASSWORD_FAILURES.keys())
async def test_a_422_never_echoes_the_submitted_password(client, case):
    path, body = case
    # json.dumps writes a lone surrogate as its escape, which json= cannot send.
    response = await client.post(path, content=json.dumps(body).encode(), headers=JSON)
    assert response.status_code == 422, response.text
    assert _PW not in response.text and _SHORT_PW not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}


async def test_msg_and_loc_can_still_quote_submitted_text(make_client):
    """Known and accepted (IAM-HASH-9): the handler drops `input` and `ctx`, not
    every byte a caller sent. `msg` is the validator's own text and can quote part
    of an email or UUID; `loc` names the keys the caller sent. None of these is a
    password field. A change here is a change to the rule, not a test fix."""
    async with make_client(mock_user(Role.ADMIN)) as ac:
        bad_char = await ac.post(
            "/register", json={**_REGISTER_OK, "email": "a@sec_ret.com", "password": _PW}
        )
        ip_literal = await ac.post(
            "/register", json={**_REGISTER_OK, "email": "secret@[1.2.3.999]", "password": _PW}
        )
        bad_uuid = await ac.get("/groups/Zz-not-a-uuid")
        extra_key = await ac.patch("/me/profile", json={"MyPassw0rd!": "x"})
    for response in (bad_char, ip_literal, bad_uuid, extra_key):
        assert response.status_code == 422, response.text
        assert _PW not in response.text
    assert "'_'" in bad_char.json()["detail"][0]["msg"]
    assert "1.2.3.999" in ip_literal.json()["detail"][0]["msg"]
    assert "`Z`" in bad_uuid.json()["detail"][0]["msg"]
    assert extra_key.json()["detail"][0]["loc"] == ["body", "MyPassw0rd!"]


async def test_a_surrogate_dict_key_is_422_not_500(make_client):
    """A lone surrogate in a key, not a value: pydantic refuses the key at the
    body's own `loc`, and the default handler echoed the whole body (a 500)."""
    async with make_client(mock_user()) as ac:
        response = await ac.patch("/me/profile", content=b'{"\\ud800": "x"}', headers=JSON)
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("string_unicode", ["body"])
