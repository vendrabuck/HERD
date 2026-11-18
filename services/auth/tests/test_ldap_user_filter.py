"""LDAP_USER_FILTER is a strict template, and a defect in it is a 503 (issue #1446).

The invariant: a misconfigured LDAP_USER_FILTER can never surface as a 500 at login.
It is reported at startup (one ERROR in LDAP mode, auth still boots) and again at
every login, as one ERROR naming the setting, the defect, and the class, never the
login name; and the login gets the answer of every other "directory cannot be asked"
state, the 503 of IAM-LDAP-12, before any directory call and before anything that
depends on the username or password, so the answer is the same for every login.

Before the fix the template went through `str.format`, so `(uid={user})` raised
KeyError, `(uid={0})` IndexError, and a single brace ValueError, none of which the
login path caught: every LDAP login answered 500.

Input-dependent behavior runs through a real ldap3 `Connection` (`MOCK_SYNC`,
tests/_mock_directory.py), which builds and parses the filter exactly as it would
for a real directory.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings, settings
from app.models.user import User
from app.services import ldap_service
from sqlalchemy import func, select

from tests import _mock_directory
from tests._harness import TestSessionLocal

KNOWN, UNKNOWN = "alice", "nobody"
GOOD = "right-password"
UNAVAILABLE = {"detail": "Directory unavailable, credentials not checked"}

# Every defect class the template can have, with the reason code and the class the
# ERROR line names.
DEFECTS = {
    "unknown_named_field": ("(uid={user})", "unknown_field", "LdapUserFilterError"),
    "positional_field": ("(uid={0})", "unknown_field", "LdapUserFilterError"),
    "empty_field": ("(uid={})", "unknown_field", "LdapUserFilterError"),
    "attribute_access": ("(uid={username.__class__})", "unknown_field", "LdapUserFilterError"),
    "index_access": ("(uid={username[0]})", "unknown_field", "LdapUserFilterError"),
    "conversion": ("(uid={username!r})", "unknown_field", "LdapUserFilterError"),
    "format_spec": ("(uid={username:>20})", "unknown_field", "LdapUserFilterError"),
    "nested_spec": ("(uid={username:{user}})", "unknown_field", "LdapUserFilterError"),
    "padded_name": ("(uid={ username })", "unknown_field", "LdapUserFilterError"),
    "stray_open_brace": ("(uid={username}){", "unbalanced_brace", "ValueError"),
    "stray_close_brace": ("(uid={username}})", "unbalanced_brace", "ValueError"),
    "unclosed_field": ("(uid={username)", "unbalanced_brace", "ValueError"),
    "no_field": ("(uid=alice)", "no_username_field", "LdapUserFilterError"),
    "only_literal_braces": ("(uid={{username}})", "no_username_field", "LdapUserFilterError"),
    "empty": ("", "no_username_field", "LdapUserFilterError"),
    "unbalanced_parenthesis": ("(uid={username}", "invalid_filter", "LDAPInvalidFilterError"),
    "no_parentheses": ("uid={username}", "invalid_filter", "LDAPInvalidFilterError"),
    "two_filters_side_by_side": (
        "(uid={username})(e2e-playwright-sentinel)",
        "invalid_filter",
        "LDAPInvalidFilterError",
    ),
    "no_assertion": ("(&(uid={username})())", "invalid_filter", "LDAPInvalidFilterError"),
}

USABLE = [
    "(uid={username})",
    "(sAMAccountName={username})",
    "(&(objectClass=person)(uid={username}))",
    "(|(uid={username})(mail={username}))",
    "(uid={username:})",
    "(description={{literal}}{username})",
    "(cn=*{username}*)",
]


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


# -- 1. The template rules ---------------------------------------------------------------


@pytest.mark.parametrize("template,reason,cls", DEFECTS.values(), ids=DEFECTS.keys())
def test_every_defect_is_found_with_its_reason(template, reason, cls):
    problem = ldap_service.user_filter_problem(template)
    assert problem is not None
    assert problem.reason == reason
    assert type(problem.__cause__ or problem).__name__ == cls
    assert "LDAP_USER_FILTER" in str(problem)


@pytest.mark.parametrize("template", USABLE)
def test_a_usable_template_has_no_problem(template):
    assert ldap_service.user_filter_problem(template) is None


def test_the_default_is_usable():
    assert (
        ldap_service.user_filter_problem(Settings.model_fields["ldap_user_filter"].default) is None
    )


def test_render_escapes_the_name_and_reads_doubled_braces_as_literal():
    rendered = ldap_service.render_user_filter(
        "(|(uid={username})(description={{x}}{username}))", "a*(b)\\"
    )
    assert rendered == "(|(uid=a\\2a\\28b\\29\\5c)(description={x}a\\2a\\28b\\29\\5c))"


@pytest.mark.parametrize("name", ["{0}", "{username}", "{user.__class__}", "}{", "%s"])
def test_render_never_interprets_the_login_name(name):
    assert ldap_service.render_user_filter("(uid={username})", name) == f"(uid={name})"


@pytest.mark.parametrize("template,reason,cls", DEFECTS.values(), ids=DEFECTS.keys())
def test_the_search_itself_never_raises_anything_but_unavailable(
    directory, monkeypatch, template, reason, cls
):
    """The backstop inside _search_user, for a caller that skipped the pre-check:
    a template defect is LdapUnavailableError there too, never KeyError,
    IndexError, or ValueError."""
    monkeypatch.setattr(settings, "ldap_user_filter", template)
    with pytest.raises(ldap_service.LdapUnavailableError):
        ldap_service._search_user(KNOWN)


# -- 2. A login in that state --------------------------------------------------------------


@pytest.mark.parametrize("template,reason,cls", DEFECTS.values(), ids=DEFECTS.keys())
def test_a_login_gets_the_same_outage_for_every_name_and_password(
    directory, monkeypatch, caplog, template, reason, cls
):
    monkeypatch.setattr(settings, "ldap_user_filter", template)
    shapes = set()
    for username, password in ((KNOWN, GOOD), (KNOWN, "wrong"), (UNKNOWN, GOOD), (KNOWN, "")):
        directory.connections.clear()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
            assert _outcome(username, password) == "outage"
        assert directory.connections == [], "no directory call with an unusable filter"
        shape = tuple(
            (r.levelname, r.action, r.setting, r.reason, r.exception_class) for r in caplog.records
        )
        shapes.add(shape)
        for record in caplog.records:
            assert getattr(record, "username", None) is None
            assert username not in record.getMessage() or username == ""
    assert shapes == {(("ERROR", "ldap_user_filter_invalid", "LDAP_USER_FILTER", reason, cls),)}


def test_a_usable_template_still_signs_in_and_refuses(directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_user_filter", "(&(objectClass=person)(uid={username}))")
    assert _outcome(KNOWN, GOOD) == "identity"
    assert _outcome(KNOWN, "wrong") == "refusal"
    assert _outcome(UNKNOWN, GOOD) == "refusal"


def test_a_name_holding_template_syntax_is_only_a_name(directory):
    directory.add_user("{0}", GOOD)
    assert _outcome("{0}", GOOD) == "identity"
    assert _outcome("{username}", GOOD) == "refusal"


# -- 3. At startup ---------------------------------------------------------------------------


def _ldap_settings(**changes) -> Settings:
    return settings.model_copy(update={"auth_method": "ldap", **changes})


@pytest.mark.parametrize("template,reason,cls", DEFECTS.values(), ids=DEFECTS.keys())
def test_startup_reports_a_defect_once_in_ldap_mode(caplog, template, reason, cls):
    with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
        fired = ldap_service.log_ldap_startup_problems(_ldap_settings(ldap_user_filter=template))
    assert fired == ["ldap_user_filter_invalid"]
    assert [(r.levelname, r.action, r.reason, r.exception_class) for r in caplog.records] == [
        ("ERROR", "ldap_user_filter_invalid", reason, cls)
    ]
    assert "LDAP_USER_FILTER" in caplog.records[0].getMessage()


@pytest.mark.parametrize("mode", ["local", "oidc"])
def test_startup_is_silent_outside_ldap_mode(caplog, mode):
    with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
        fired = ldap_service.log_ldap_startup_problems(
            _ldap_settings(auth_method=mode, ldap_user_filter="(uid={user})")
        )
    assert fired == []
    assert caplog.records == []


def test_startup_is_silent_for_a_usable_template(caplog):
    with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
        assert ldap_service.log_ldap_startup_problems(_ldap_settings()) == []
    assert caplog.records == []


async def test_the_lifespan_reports_it_and_still_boots(monkeypatch, caplog):
    from app.main import lifespan

    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={user})", raising=False)
    with (
        patch("app.main.create_all_and_stamp", AsyncMock()),
        patch("app.main._seed_superadmin", AsyncMock()),
        patch("app.main._seed_not_grouped", AsyncMock()),
        patch("app.main.ldap_group_sync_loop_enabled", return_value=False),
        caplog.at_level(logging.ERROR, logger="app.services.ldap_service"),
    ):
        async with lifespan(MagicMock()):
            booted = True
    assert booted
    assert [getattr(r, "action", None) for r in caplog.records] == ["ldap_user_filter_invalid"]


# -- 4. Through the route --------------------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _user_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar_one()


@pytest.mark.parametrize("template", ["(uid={user})", "(uid={0})", "(uid={username}){"])
async def test_the_route_answers_503_never_500(client, directory, monkeypatch, template):
    monkeypatch.setattr(settings, "ldap_user_filter", template)
    answers = []
    for username in (KNOWN, UNKNOWN):
        response = await client.post("/login", json={"email": username, "password": GOOD})
        answers.append((response.status_code, response.json()))
    assert answers == [(503, UNAVAILABLE), (503, UNAVAILABLE)]
    assert directory.connections == []
    assert await _user_count() == 0
