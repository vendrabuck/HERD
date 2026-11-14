"""Direct-bcrypt password hashing, replacing passlib (issue #1355).

The contract (`app/services/passwords.py`): `verify_password` accepts exactly the
(password, stored hash) pairs passlib 1.7.4's `CryptContext(schemes=["bcrypt"],
deprecated="auto")` accepted, returns False for every other pair, never raises, and
always costs exactly one bcrypt computation. `tests/data/passlib_1_7_4_fixtures.json`
holds 802 decisions recorded by running passlib itself on the pre-#1355 commit (see
its generator); the matrix test below replays every one.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import bcrypt
import pytest
from app.models.user import User
from app.services import auth_service, passwords
from sqlalchemy import select

from tests._harness import TestSessionLocal

DATA = Path(__file__).parent / "data"
LEGACY = json.loads((DATA / "passlib_1_7_4_fixtures.json").read_text())
LEGACY_PASSWORDS: dict[str, str] = LEGACY["passwords"]
LEGACY_CASES: list[dict] = LEGACY["cases"]

INVALID = {"detail": "Invalid credentials"}
SERVICE_DIR = Path(__file__).resolve().parents[1]


def _cost(stored: str) -> int:
    return int(stored.split("$")[2])


def _ident(stored: str) -> str:
    return stored.split("$")[1]


@pytest.fixture
def cheap_dummy(monkeypatch):
    """A cost-4 dummy for the large matrices: the dummy check's result is never
    used, so its cost changes only the run time, never a decision."""
    monkeypatch.setattr(passwords, "_DUMMY_HASH_BYTES", bcrypt.hashpw(b"x", bcrypt.gensalt(4)))


@pytest.fixture
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(passwords, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def bcrypt_spy(monkeypatch) -> dict[str, list]:
    """Every bcrypt computation, by entry point: the secret and hash checkpw got,
    and the secret hashpw got."""
    calls: dict[str, list] = {"checkpw": [], "hashpw": [], "gensalt": []}
    real_checkpw, real_hashpw, real_gensalt = bcrypt.checkpw, bcrypt.hashpw, bcrypt.gensalt

    def checkpw(secret, hashed):
        calls["checkpw"].append((secret, hashed))
        return real_checkpw(secret, hashed)

    def hashpw(secret, salt):
        calls["hashpw"].append(secret)
        return real_hashpw(secret, salt)

    def gensalt(*args, **kwargs):
        calls["gensalt"].append((args, kwargs))
        return real_gensalt(*args, **kwargs)

    monkeypatch.setattr(bcrypt, "checkpw", checkpw)
    monkeypatch.setattr(bcrypt, "hashpw", hashpw)
    monkeypatch.setattr(bcrypt, "gensalt", gensalt)
    return calls


# -- 1. Every decision passlib made is kept -------------------------------------


def test_the_legacy_table_came_from_the_production_passlib_configuration():
    provenance = LEGACY["provenance"]
    assert provenance["passlib"] == "1.7.4"
    assert provenance["bcrypt"] == "4.0.1"
    assert provenance["production_context"] == 'CryptContext(schemes=["bcrypt"], deprecated="auto")'
    # New hashes keep passlib's defaults.
    assert provenance["production_default_ident"] == "$" + passwords.BCRYPT_IDENT.decode() + "$"
    assert provenance["production_default_rounds"] == passwords.BCRYPT_ROUNDS
    groups = {case["group"] for case in LEGACY_CASES}
    assert groups == {"production", "cross", "mutation", "ident", "cost", "malformed", "padding"}
    production = [c for c in LEGACY_CASES if c["group"] == "production"]
    assert {_ident(c["hash"]) for c in production} == {"2b"}
    assert {_cost(c["hash"]) for c in production} == {12}


def test_every_legacy_passlib_decision_is_kept(cheap_dummy):
    """A pair passlib accepted is accepted; a pair it refused or raised on is
    refused, with False and no exception. One test over all 802 cases (each a
    parametrized test would pay the suite's per-test schema setup), naming every
    case that disagrees."""
    disagreements = []
    for case in LEGACY_CASES:
        password = LEGACY_PASSWORDS[case["password_key"]]
        expected = case["passlib"] == "accept"
        if auth_service.verify_password(password, case["hash"]) is not expected:
            disagreements.append(f"{case['group']}:{case['label']} (passlib {case['passlib']})")
    assert disagreements == []
    assert len(LEGACY_CASES) == 802


@pytest.mark.parametrize(
    "entry", LEGACY["refused_to_hash"], ids=[e["label"] for e in LEGACY["refused_to_hash"]]
)
def test_a_password_passlib_refused_to_hash_is_still_refused(entry):
    assert entry["passlib"] == "raise"
    with pytest.raises(ValueError):
        auth_service.get_password_hash(entry["password"])


# -- 2. The 72-byte rule ----------------------------------------------------------

_BOUNDARY_BASE = "A" * 70
_BOUNDARY_SUFFIXES = [
    "",
    "B",
    "BC",
    "BCD",
    "BCE",
    "\u00e9",  # 2 bytes: ends exactly at byte 72
    "\u00e9X",
    "\u00e9Y",
    "\u65e5",  # 3 bytes: the cut falls inside it
    "\u65e6",  # shares its first two bytes with U+65E5
    "\u65e5\u65e5",
    "\U0001f511",  # 4 bytes
    "\U0001f512",
]
_BOUNDARY = [_BOUNDARY_BASE + s for s in _BOUNDARY_SUFFIXES] + [
    "\U0001f511" * 30,
    "\U0001f511" * 18,
]


def test_two_passwords_match_exactly_when_their_first_72_utf8_bytes_match(
    cheap_hashes, cheap_dummy
):
    """verify(q, hash(p)) == (utf8(p)[:72] == utf8(q)[:72]) over every pair around
    the boundary, the cut inside a multi-byte character included. This is the rule
    passlib applied silently and this module applies explicitly."""
    hashes = {p: passwords.hash_password(p) for p in _BOUNDARY}
    for p, stored in hashes.items():
        for q in _BOUNDARY:
            same_prefix = p.encode()[:72] == q.encode()[:72]
            assert passwords.verify_password(q, stored) is same_prefix, (len(p), len(q))


@pytest.mark.parametrize(
    "password",
    [
        "",
        "a",
        "a" * 71,
        "a" * 72,
        "a" * 73,
        "\u00e9" * 36,
        "\u00e9" * 37,
        "\U0001f511" * 30,
        "q" * 4096,
    ],
    ids=["0B", "1B", "71B", "72B", "73B", "72B-2byte", "74B-2byte", "120B-4byte", "4096B"],
)
def test_bcrypt_never_receives_more_than_the_first_72_bytes(password, bcrypt_spy, cheap_hashes):
    stored = passwords.hash_password(password)
    assert passwords.verify_password(password, stored) is True
    expected = password.encode()[:72]
    assert bcrypt_spy["hashpw"] == [expected]
    assert [secret for secret, _ in bcrypt_spy["checkpw"]] == [expected]


# -- 3. Refused passwords and unusable stored hashes --------------------------------

_REFUSED_PASSWORDS = {
    "nul_inside": "ab\x00cd",
    "nul_only": "\x00",
    "nul_after_byte_72": "a" * 80 + "\x00",
    "4097_bytes": "\u00e9" * 2048 + "a",
    "lone_surrogate": "ab\ud800cd",
    "not_a_string": b"bytes-password",
    "none": None,
}


@pytest.mark.parametrize("password", _REFUSED_PASSWORDS.values(), ids=_REFUSED_PASSWORDS.keys())
def test_a_refused_password_never_hashes(password):
    with pytest.raises(ValueError):
        passwords.hash_password(password)


@pytest.mark.parametrize("password", _REFUSED_PASSWORDS.values(), ids=_REFUSED_PASSWORDS.keys())
def test_a_refused_password_is_false_after_one_dummy_check(password, bcrypt_spy):
    # A real production-cost hash whose first 72 bytes match several entries above.
    stored = passwords.hash_password("a" * 80)
    bcrypt_spy["checkpw"].clear()
    assert passwords.verify_password(password, stored) is False
    assert bcrypt_spy["checkpw"] == [(b"", passwords.DUMMY_HASH.encode())]


@pytest.mark.parametrize("password", _REFUSED_PASSWORDS.values(), ids=_REFUSED_PASSWORDS.keys())
def test_a_refused_password_against_a_good_hash_logs_nothing(password, caplog, cheap_hashes):
    """The unusable-hash WARNING is about the stored side only: no password a
    client sends can produce it against a usable hash."""
    stored = passwords.hash_password("a" * 80)
    with caplog.at_level(logging.DEBUG):
        assert passwords.verify_password(password, stored) is False
    assert [r for r in caplog.records if r.name == "app.services.passwords"] == []


def test_4096_bytes_is_still_accepted(cheap_hashes):
    password = "\u00e9" * 2048
    assert len(password.encode()) == passwords.MAX_PASSWORD_BYTES
    assert passwords.verify_password(password, passwords.hash_password(password)) is True


_VALID = "$2b$04$" + "abcdefghijklmnopqrstuu" + "a" * 31
_UNUSABLE_HASHES = {
    "none": None,
    "empty": "",
    "garbage": "garbage",
    "bytes": _VALID.encode(),
    "integer": 12345,
    "truncated": _VALID[:40],
    "one_char_short": _VALID[:-1],
    "one_char_long": _VALID + "a",
    "trailing_newline": _VALID + "\n",
    "ident_2x": "$2x$" + _VALID[4:],
    "ident_2c": "$2c$" + _VALID[4:],
    "uppercase_ident": "$2B$" + _VALID[4:],
    "cost_03": "$2b$03$" + _VALID[7:],
    "cost_32": "$2b$32$" + _VALID[7:],
    "cost_single_digit": "$2b$4$" + _VALID[7:],
    "unicode_digit_cost": "$2b$\u0661\u0662$" + _VALID[7:],
    "non_ascii": _VALID[:-1] + "\u00e9",
    "md5_crypt": "$1$saltsalt$2vN3oTUt4pk6p9I4j6CT1/",
}


@pytest.mark.parametrize("stored", _UNUSABLE_HASHES.values(), ids=_UNUSABLE_HASHES.keys())
def test_an_unusable_stored_hash_is_false_after_one_dummy_check(stored, bcrypt_spy, caplog):
    password = "s3cret-for-the-log-check"
    with caplog.at_level(logging.DEBUG):
        assert passwords.verify_password(password, stored) is False
    assert bcrypt_spy["checkpw"] == [(b"", passwords.DUMMY_HASH.encode())]
    records = [r for r in caplog.records if r.name == "app.services.passwords"]
    assert [(r.levelno, r.getMessage(), r.action) for r in records] == [
        (logging.WARNING, passwords.UNUSABLE_HASH_MESSAGE, "password_hash_unusable")
    ]
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert password not in text
        if isinstance(stored, str) and len(stored) > 8:
            assert stored not in text


def test_a_bcrypt_error_on_an_admitted_hash_is_false_after_one_dummy_check(monkeypatch, caplog):
    """The backstop: should bcrypt ever refuse a hash the pattern admitted, the
    answer is still False after one dummy check, logged by class name only."""
    real = bcrypt.checkpw
    seen: list[bytes] = []

    def refusing_checkpw(secret, hashed):
        seen.append(hashed)
        if hashed != passwords.DUMMY_HASH.encode():
            raise ValueError("Invalid salt " + hashed.decode())
        return real(secret, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", refusing_checkpw)
    with caplog.at_level(logging.WARNING):
        assert passwords.verify_password("pw", _VALID) is False
    assert seen == [_VALID.encode(), passwords.DUMMY_HASH.encode()]
    (record,) = [r for r in caplog.records if r.name == "app.services.passwords"]
    assert (record.getMessage(), record.error) == (passwords.UNUSABLE_HASH_MESSAGE, "ValueError")
    assert _VALID not in record.getMessage() + repr(record.__dict__)


# -- 4. Timing: one bcrypt per check, the dummy at the configured cost ----------------


def test_the_dummy_hash_has_the_ident_and_cost_of_every_new_hash():
    new = passwords.hash_password("anything")
    for stored in (new, passwords.DUMMY_HASH, auth_service._DUMMY_HASH):
        assert len(stored) == 60
        assert _ident(stored) == passwords.BCRYPT_IDENT.decode() == "2b"
        assert _cost(stored) == passwords.BCRYPT_ROUNDS == 12
    assert auth_service._DUMMY_HASH == passwords.DUMMY_HASH
    assert passwords.verify_password("not-a-real-password", passwords.DUMMY_HASH) is True
    assert passwords.verify_password("anything", passwords.DUMMY_HASH) is False


@pytest.mark.parametrize("cost", [4, 10, 12])
def test_a_usable_hash_is_checked_once_at_its_own_cost_and_never_rehashed(cost, bcrypt_spy):
    stored = bcrypt.hashpw(b"pw", bcrypt.gensalt(cost)).decode()
    bcrypt_spy["checkpw"].clear()
    bcrypt_spy["hashpw"].clear()
    bcrypt_spy["gensalt"].clear()
    assert passwords.verify_password("pw", stored) is True
    assert passwords.verify_password("wrong", stored) is False
    assert [(s, _cost(h.decode())) for s, h in bcrypt_spy["checkpw"]] == [
        (b"pw", cost),
        (b"wrong", cost),
    ]
    assert bcrypt_spy["hashpw"] == [] and bcrypt_spy["gensalt"] == []


# -- 5. Through the real API ---------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _add_user(email: str, stored_hash: str | None, *, active: bool = True, source="local"):
    async with TestSessionLocal() as db:
        db.add(
            User(
                email=email,
                username=email.split("@")[0].replace(".", "-")[:32],
                full_name="Legacy",
                hashed_password=stored_hash,
                auth_source=source,
                is_active=active,
            )
        )
        await db.commit()


_PRODUCTION_LOGINS = [
    c
    for c in LEGACY_CASES
    if c["group"] == "production" and len(LEGACY_PASSWORDS[c["password_key"]]) <= 72
]


@pytest.mark.parametrize("case", _PRODUCTION_LOGINS, ids=[c["label"] for c in _PRODUCTION_LOGINS])
async def test_login_with_a_hash_passlib_stored_decides_as_passlib_did(case, client):
    """A user whose row holds a hash the old production code wrote signs in
    exactly when passlib would have let them."""
    await _add_user("legacy@example.test", case["hash"])
    password = LEGACY_PASSWORDS[case["password_key"]]
    response = await client.post(
        "/login", json={"email": "legacy@example.test", "password": password}
    )
    if case["passlib"] == "accept":
        assert response.status_code == 200, response.text
    else:
        assert (response.status_code, response.json()) == (401, INVALID)


async def test_a_30_character_password_over_72_bytes_registers_and_signs_in_on_its_first_72_bytes(
    client,
):
    password = "\U0001f511" * 30  # 30 characters, 120 UTF-8 bytes
    register = await client.post(
        "/register", json={"email": "keys@example.com", "username": "keys", "password": password}
    )
    assert register.status_code == 201, register.text

    async def login(pw: str) -> int:
        resp = await client.post("/login", json={"email": "keys@example.com", "password": pw})
        return resp.status_code

    assert await login(password) == 200
    assert await login("\U0001f511" * 18) == 200  # the same first 72 bytes
    assert await login("\U0001f511" * 17) == 401
    async with TestSessionLocal() as db:
        stored = (await db.execute(select(User.hashed_password))).scalar_one()
    assert (_ident(stored), _cost(stored)) == ("2b", 12)


async def test_a_login_with_a_nul_password_is_401_not_500(client):
    """passlib raised PasswordValueError here, a 500; the refusal is now a 401."""
    await _add_user("nul@example.test", passwords.hash_password("right-password"))
    for email in ("nul@example.test", "nobody@example.test"):
        response = await client.post(
            "/login", json={"email": email, "password": "right\x00password"}
        )
        assert (response.status_code, response.json()) == (401, INVALID)


async def test_a_login_against_an_unusable_stored_hash_is_401_not_500(client, caplog):
    """passlib raised on a malformed stored hash, a 500; the refusal is now the
    same 401 as a wrong password, and the log names neither hash nor password."""
    await _add_user("broken@example.test", "$2b$12$not-a-real-hash")
    with caplog.at_level(logging.WARNING):
        response = await client.post(
            "/login", json={"email": "broken@example.test", "password": "pw-for-broken"}
        )
    assert (response.status_code, response.json()) == (401, INVALID)
    assert any(r.getMessage() == passwords.UNUSABLE_HASH_MESSAGE for r in caplog.records)
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert "not-a-real-hash" not in text and "pw-for-broken" not in text


async def test_a_login_at_another_cost_succeeds_and_leaves_the_stored_hash_alone(client):
    stored = bcrypt.hashpw(b"low-cost-password", bcrypt.gensalt(4)).decode()
    await _add_user("lowcost@example.test", stored)
    response = await client.post(
        "/login", json={"email": "lowcost@example.test", "password": "low-cost-password"}
    )
    assert response.status_code == 200
    async with TestSessionLocal() as db:
        assert (await db.execute(select(User.hashed_password))).scalar_one() == stored


_LOGIN_PATHS = {
    "success": ("ok@example.test", "right-password", 200),
    "wrong_password": ("ok@example.test", "wrong-password", 401),
    "unknown_email": ("nobody@example.test", "right-password", 401),
    "inactive_right_password": ("off@example.test", "right-password", 401),
    "ldap_account": ("ldap@example.test", "right-password", 401),
    "local_without_hash": ("nohash@example.test", "right-password", 401),
    "unusable_hash": ("broken@example.test", "right-password", 401),
    "nul_password": ("ok@example.test", "right\x00password", 401),
}


@pytest.mark.parametrize("path", _LOGIN_PATHS.values(), ids=_LOGIN_PATHS.keys())
async def test_every_local_login_answer_costs_one_bcrypt_at_the_configured_cost(
    path, client, bcrypt_spy
):
    """IAM-LOGIN-2 at the bcrypt layer: each answer, admitted or refused, runs
    exactly one bcrypt computation, at cost 12, and never hashes anew."""
    good = passwords.hash_password("right-password")
    await _add_user("ok@example.test", good)
    await _add_user("off@example.test", good, active=False)
    await _add_user("ldap@example.test", None, source="ldap")
    await _add_user("nohash@example.test", None)
    await _add_user("broken@example.test", "garbage")
    for calls in bcrypt_spy.values():
        calls.clear()
    email, password, status = path
    response = await client.post("/login", json={"email": email, "password": password})
    assert response.status_code == status
    assert len(bcrypt_spy["checkpw"]) == 1
    assert _cost(bcrypt_spy["checkpw"][0][1].decode()) == passwords.BCRYPT_ROUNDS
    assert bcrypt_spy["hashpw"] == [] and bcrypt_spy["gensalt"] == []


# -- 6. Dependencies ------------------------------------------------------------------


def test_passlib_is_not_installed():
    import importlib.util

    assert importlib.util.find_spec("passlib") is None


def test_import_and_first_use_raise_no_warning():
    """Run in a fresh interpreter with every warning an error: passlib's bcrypt
    `__about__` and `crypt` warnings were part of why #1355 exists."""
    code = (
        "import logging\n"
        "logging.disable(logging.CRITICAL)\n"  # stderr then holds warnings only
        "from app.services import passwords\n"
        "h = passwords.hash_password('pw')\n"
        "assert passwords.verify_password('pw', h)\n"
        "assert not passwords.verify_password('pw', 'garbage')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONWARNINGS"}
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", code],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
