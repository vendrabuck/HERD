"""An LDAP login signs in only as the single entry the filter matches (issue #1447).

The invariant: an LDAP login signs in only as the single entry `LDAP_USER_FILTER`
matches for that name; zero matches and more than one match are both refusals
that look the same to the client.

Before the fix `_search_user` took `entries[0]`, so of two entries the filter
matched for one name the second person could never sign in, and which one won was
the directory's ordering. Now the login search asks for at most two entries, and
a name that matches more than one is the 401 `Invalid credentials` whatever the
password, with no bind as either entry (no password tried, no lockout counter
touched) and one ERROR naming the setting and the count, never a DN.

Everything runs through a real ldap3 `Connection` (`MOCK_SYNC`,
tests/_mock_directory.py): which entries match depends on the submitted name, so
the filter has to be built and evaluated by ldap3 itself. `MOCK_SYNC` truncates
a size-limited search silently (success, never sizeLimitExceeded), so these
tests also pin that two entries are asked for: with a limit of one, the second
entry would never arrive. The result codes a real directory adds are scripted in
test_ldap_service.py and met for real in test_ldap_service_live.py.
"""

from __future__ import annotations

import logging

import pytest
from app.config import settings
from app.models.user import User
from app.services import ldap_service
from sqlalchemy import func, select

from tests import _mock_directory
from tests._harness import TestSessionLocal

BASE = _mock_directory.BASE_DN
SERVICE = _mock_directory.SERVICE_DN
TWIN = "twin"
TWIN_A_DN = f"cn=twin-a,{BASE}"
TWIN_B_DN = f"cn=twin-b,{BASE}"
TWIN_A_PW, TWIN_B_PW = "twin-a-password", "twin-b-password"
ALICE_PW = "alice-password"


@pytest.fixture
def directory(monkeypatch):
    mock = _mock_directory.configure(monkeypatch, settings, ldap_service)
    # Two people whose uid is the same (the RDN differs, so both can exist).
    for dn, password in ((TWIN_A_DN, TWIN_A_PW), (TWIN_B_DN, TWIN_B_PW)):
        mock.add_entry(
            dn,
            {
                "uid": TWIN,
                "mail": f"{dn.split(',')[0][3:]}@example.com",
                "userPassword": password,
                "objectClass": "person",
            },
        )
    mock.add_user("alice", ALICE_PW)
    mock.add_user("alicia", "alicia-password")
    return mock


def _outcome(username: str, password: str):
    try:
        identity = ldap_service._bind_user_sync(username, password)
    except ldap_service.LdapUnavailableError:
        return "outage"
    return identity.dn if identity else "refusal"


def _no_dn_or_address_in(records) -> None:
    for record in records:
        for value in (record.getMessage(), *vars(record).values()):
            if isinstance(value, str):
                for secret in (TWIN_A_DN, TWIN_B_DN, "twin-a@", "twin-b@", "alice@", "alicia@"):
                    assert secret not in value, (record.getMessage(), value)


# -- 1. The client ------------------------------------------------------------------


def test_the_mock_directory_holds_two_entries_for_one_name(directory):
    """The precondition: the filter really matches both, and each entry's own
    password is right for it (bound directly, as the user bind would)."""
    conn = directory.connection(None, user=SERVICE, password=_mock_directory.SERVICE_PASSWORD)
    conn.bind()
    conn.search(BASE, f"(uid={TWIN})", attributes=["uid"])
    assert sorted(e.entry_dn for e in conn.entries) == sorted([TWIN_A_DN, TWIN_B_DN])
    assert ldap_service._bind_as_user(TWIN_A_DN, TWIN_A_PW) is True
    assert ldap_service._bind_as_user(TWIN_B_DN, TWIN_B_PW) is True


@pytest.mark.parametrize("password", [TWIN_A_PW, TWIN_B_PW, "wrong-password"])
def test_a_name_two_entries_match_is_refused_whatever_the_password(directory, password, caplog):
    directory.connections.clear()
    directory.binds.clear()
    with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
        assert _outcome(TWIN, password) == "refusal"
    # One connection and one bind, both the service account's: no entry's
    # password was tried, so no lockout counter was touched.
    assert directory.connections == [SERVICE]
    assert directory.binds == [SERVICE]
    assert [(r.levelname, r.action) for r in caplog.records] == [("ERROR", "ldap_user_ambiguous")]
    (record,) = caplog.records
    assert (record.setting, record.count) == ("LDAP_USER_FILTER", 2)
    assert record.username == TWIN
    assert "LDAP_USER_FILTER" in record.getMessage()
    _no_dn_or_address_in(caplog.records)


def test_an_ambiguous_name_costs_what_an_unknown_name_costs(directory):
    """The timing class: one service connection, one bind, one search, and no
    user bind, the same calls as a name no entry matches."""
    calls = {}
    for name in (TWIN, "nobody"):
        directory.connections.clear()
        directory.binds.clear()
        assert _outcome(name, TWIN_A_PW) == "refusal"
        calls[name] = (list(directory.connections), list(directory.binds))
    assert calls[TWIN] == calls["nobody"] == ([SERVICE], [SERVICE])


def test_one_entry_still_signs_in(directory):
    directory.binds.clear()
    assert _outcome("alice", ALICE_PW) == f"uid=alice,{BASE}"
    assert directory.binds == [SERVICE, f"uid=alice,{BASE}"]
    assert _outcome("alice", "wrong-password") == "refusal"


@pytest.mark.parametrize(
    "name,password",
    [("ali", ALICE_PW), ("ali", "alicia-password"), ("al", "wrong-password")],
)
def test_a_substring_filter_refuses_a_prefix_two_entries_match(
    directory, monkeypatch, caplog, name, password
):
    """IAM-LDAP-14 allows `(uid={username}*)`; before #1447 the prefix `ali` signed
    in as whichever of alice and alicia the directory listed first, with that
    entry's password. Now it is refused, and a name that matches one entry
    still signs in as that entry."""
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username}*)")
    directory.binds.clear()
    with caplog.at_level(logging.ERROR, logger="app.services.ldap_service"):
        assert _outcome(name, password) == "refusal"
    assert directory.binds == [SERVICE]
    assert [r.action for r in caplog.records] == ["ldap_user_ambiguous"]
    _no_dn_or_address_in(caplog.records)


def test_a_substring_filter_still_signs_in_a_name_one_entry_matches(directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username}*)")
    assert _outcome("alici", "alicia-password") == f"uid=alicia,{BASE}"
    assert _outcome("alice", ALICE_PW) == f"uid=alice,{BASE}"


def test_a_search_result_reference_is_not_an_entry(directory, monkeypatch):
    """A continuation reference into another server's part of the tree is not an
    entry (ldap3 leaves it out of `entries`), so one entry beside one still
    signs in, and the reference is not followed. Active Directory sends such
    references on many searches from the domain root, so counting them would
    refuse every login there."""
    real_connection = directory.connection

    def with_reference(server, **kwargs):
        conn = real_connection(server, **kwargs)
        search = conn.search

        def search_with_reference(*args, **search_kwargs):
            found = search(*args, **search_kwargs)
            conn.response.append(
                {"type": "searchResRef", "uri": ["ldap://other.example.com/dc=example,dc=com??sub"]}
            )
            conn._entries = []
            return found

        conn.search = search_with_reference
        return conn

    monkeypatch.setattr(ldap_service, "Connection", with_reference)
    assert _outcome("alice", ALICE_PW) == f"uid=alice,{BASE}"
    assert directory.connections[-1] == f"uid=alice,{BASE}"
    assert all(user in (SERVICE, f"uid=alice,{BASE}") for user in directory.connections)


# -- 2. Through the route -----------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def _answer(client, name: str, password: str):
    response = await client.post("/login", json={"email": name, "password": password})
    return response.status_code, response.json(), dict(response.headers)


async def test_the_route_answers_an_ambiguous_name_like_an_unknown_name_and_a_wrong_password(
    client, directory, caplog
):
    """No existence or ambiguity oracle: status, body, and headers are the same
    for the first entry's password, the second's, a wrong one, and an empty one,
    and the same as for an unknown name and for a wrong password of a name that
    matches one entry. Nothing is bound as either entry and no account is made."""
    refusal = await _answer(client, "nobody", "wrong-password")
    assert refusal[:2] == (401, {"detail": "Invalid credentials"})
    assert refusal[2]["www-authenticate"] == "Bearer"
    assert await _answer(client, "alice", "wrong-password") == refusal
    for password in (TWIN_A_PW, TWIN_B_PW, "wrong-password", ""):
        directory.connections.clear()
        directory.binds.clear()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert await _answer(client, TWIN, password) == refusal
        # An empty password is refused before any call (IAM-LDAP-4), as for every name.
        expected = [] if password == "" else [SERVICE]
        assert directory.connections == expected
        assert directory.binds == expected
        ambiguous = [
            r for r in caplog.records if getattr(r, "action", None) == "ldap_user_ambiguous"
        ]
        assert len(ambiguous) == (0 if password == "" else 1)
        _no_dn_or_address_in(caplog.records)
    assert await _user_count() == 0


async def test_the_route_still_signs_in_a_name_one_entry_matches(client, directory):
    response = await client.post("/login", json={"email": "alice", "password": ALICE_PW})
    assert response.status_code == 200, response.text
    assert await _user_count() == 1


async def test_the_route_refuses_a_prefix_two_entries_match_under_a_substring_filter(
    client, directory, monkeypatch
):
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username}*)")
    refusal = await _answer(client, "nobody", ALICE_PW)
    assert await _answer(client, "ali", ALICE_PW) == refusal
    assert await _answer(client, "ali", "alicia-password") == refusal
    assert await _user_count() == 0
    response = await client.post("/login", json={"email": "alici", "password": "alicia-password"})
    assert response.status_code == 200, response.text
