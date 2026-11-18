"""No LDAP login answer depends on whether the username exists (#1403 review, HIGH).

The invariant: no answer, status, log line, or count of directory calls of an LDAP
login depends on whether the username exists, except a successful sign-in.

The regression this pins: ldap3 runs SASLprep (RFC 4013) on a simple-bind password
inside `bind()` and raises `LDAPSASLPrepError` for, among others, an ASCII control
character (tab, NUL, 0x01, DEL), a private-use code point, or mixed right-to-left
and left-to-right text. The user bind happens only when the search found the user,
so filing that error as an outage answered an EXISTING user 503 and an unknown one
401, and logged an outage WARNING per request. `_bind_user_sync` now asks ldap3's
own `validate_simple_password` before any directory call, and `_bind_as_user`
treats the error as a refusal (the second guard).

Everything runs through a real ldap3 `Connection` (`MOCK_SYNC`,
tests/_mock_directory.py), never the scripted fake, because the behavior under test
is ldap3's own client-side processing.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.models.user import User
from app.services import ldap_service
from sqlalchemy import func, select

from tests import _mock_directory
from tests._harness import TestSessionLocal

KNOWN, UNKNOWN = "alice", "nobody"
GOOD = "right-password"

# Every class ldap3's SASLprep refuses (fuzzed over every code point: these are its
# only refusals, all LDAPSASLPrepError).
REFUSED = {
    "tab": "pass\tword",
    "nul": "pass\x00word",
    "ctrl_x01": "pass\x01word",
    "del": "pass\x7fword",
    "c1_control": "pass\x80word",
    "private_use": "password",
    "mixed_bidi": "passאword",
    "rtl_not_at_both_ends": "1א",
    "non_character": "pass﷐word",
    "tagging": "pass\U000e0001word",
    "display_changing": "pass‎word",
    "maps_to_nothing": "­",
    "only_a_tab": "\t",
}


@pytest.fixture
def directory(monkeypatch):
    mock = _mock_directory.configure(monkeypatch, settings, ldap_service)
    mock.add_user(KNOWN, GOOD)
    return mock


def _outcome(username: str, password: str):
    try:
        identity = ldap_service._bind_user_sync(username, password)
    except ldap_service.LdapUnavailableError:
        return "outage"
    return "identity" if identity else "refusal"


def _log_shape(records) -> list[tuple]:
    """Each record's level, action, and class, with the username left out."""
    return [
        (r.levelname, getattr(r, "action", None), getattr(r, "exception_class", None))
        for r in records
    ]


# -- 1. The client ------------------------------------------------------------------


def test_the_mock_directory_still_tells_right_from_wrong(directory):
    assert _outcome(KNOWN, GOOD) == "identity"
    assert _outcome(KNOWN, "wrong-password") == "refusal"
    assert _outcome(UNKNOWN, "wrong-password") == "refusal"


@pytest.mark.parametrize("password", REFUSED.values(), ids=REFUSED.keys())
def test_a_password_saslprep_refuses_gets_the_same_answer_for_every_username(
    directory, password, caplog
):
    shapes = {}
    for username in (KNOWN, UNKNOWN):
        directory.connections.clear()
        caplog.clear()
        with caplog.at_level("INFO", logger="app.services.ldap_service"):
            assert _outcome(username, password) == "refusal"
        assert directory.connections == [], "no directory call before the refusal"
        shapes[username] = _log_shape(caplog.records)
        for record in caplog.records:
            assert getattr(record, "action", None) != "ldap_error"
            # Each attribute itself, not a repr of the record, which would escape
            # a tab or a NUL and hide it.
            for value in (record.getMessage(), *vars(record).values()):
                assert not (isinstance(value, str) and password in value)
            assert getattr(record, "username", username) == username
    assert (
        shapes[KNOWN]
        == shapes[UNKNOWN]
        == [("WARNING", "ldap_password_refused", "LDAPSASLPrepError")]
    )


# SASLprep MAPS these characters instead of refusing them, so they reach the
# directory in their mapped form (RFC 4013, ldap3's behavior, not HERD's): a soft
# hyphen maps to nothing, a non-breaking space to a space, and NFKC folds a
# fullwidth letter to ASCII. So "pass­word" signs in as "password", and a
# directory password that literally holds such a character can never be matched.
MAPPED = {
    "soft_hyphen": ("pass­word", "password"),
    "non_breaking_space": ("pass word", "pass word"),
    "fullwidth": ("ｐａｓｓword", "password"),
}


@pytest.mark.parametrize("submitted,sent", MAPPED.values(), ids=MAPPED.keys())
def test_a_password_saslprep_maps_goes_to_the_directory_mapped(directory, submitted, sent):
    directory.add_user("mapped", sent)
    directory.add_user("literal", submitted)
    assert _outcome("mapped", submitted) == "identity"
    assert _outcome("literal", submitted) == "refusal"
    directory.connections.clear()
    assert _outcome(UNKNOWN, submitted) == "refusal"
    # It went to the directory: the service connection searched.
    assert directory.connections == [_mock_directory.SERVICE_DN]


@pytest.mark.parametrize("password", ["pass\tword", "password", "pass\x00word"])
def test_the_user_bind_itself_refuses_what_saslprep_refuses(directory, password):
    """The second guard: reached without the pre-check, ldap3's client-side
    refusal is a refusal, never an outage."""
    dn = f"uid={KNOWN},{_mock_directory.BASE_DN}"
    assert ldap_service._bind_as_user(dn, password) is False


# -- 2. Through the route ----------------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


@pytest.mark.parametrize("password", REFUSED.values(), ids=REFUSED.keys())
async def test_the_route_answers_known_and_unknown_users_identically(
    client, directory, password, caplog
):
    answers = {}
    for username in (KNOWN, UNKNOWN):
        caplog.clear()
        with caplog.at_level("INFO"):
            response = await client.post("/login", json={"email": username, "password": password})
        reasons = [
            (r.levelname, getattr(r, "action", None), getattr(r, "reason", None))
            for r in caplog.records
            if getattr(r, "action", None)
        ]
        answers[username] = (response.status_code, response.json(), dict(response.headers), reasons)
    assert answers[KNOWN] == answers[UNKNOWN]
    status, body, headers, reasons = answers[KNOWN]
    assert (status, body) == (401, {"detail": "Invalid credentials"})
    assert headers["www-authenticate"] == "Bearer"
    assert ("WARNING", "ldap_password_refused", None) in reasons
    assert all(action != "ldap_error" for _, action, _ in reasons)
    assert directory.connections == []
    assert await _user_count() == 0


async def test_the_route_still_signs_in_through_real_ldap3(client, directory):
    response = await client.post("/login", json={"email": KNOWN, "password": GOOD})
    assert response.status_code == 200, response.text
