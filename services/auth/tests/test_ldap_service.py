"""Unit tests for ldap_service.bind_user and the sync-facing directory client.

The first tests stub the two internal helpers (_search_user, _bind_as_user) to
assert the orchestration and filter escaping. The failure-class tests (issue
#1403) replace only ldap3's Connection with the scripted directory in
tests/_fake_directory.py, so the real classification code runs: what the
directory said about a login is a refusal (None), and a directory that could not
be asked raises LdapUnavailableError. The real directory is exercised by
test_ldap_service_live.py.
"""

import ssl
from contextlib import contextmanager

import pytest
from app.config import settings
from app.services import ldap_service
from ldap3.core.exceptions import (
    LDAPException,
    LDAPInvalidFilterError,
    LDAPResponseTimeoutError,
    LDAPSocketOpenError,
    LDAPSocketReceiveError,
    LDAPStartTLSError,
)
from ldap3.core.results import (
    RESULT_BUSY,
    RESULT_CONSTRAINT_VIOLATION,
    RESULT_INAPPROPRIATE_AUTHENTICATION,
    RESULT_INSUFFICIENT_ACCESS_RIGHTS,
    RESULT_INVALID_CREDENTIALS,
    RESULT_NO_SUCH_OBJECT,
    RESULT_REFERRAL,
    RESULT_SUCCESS,
    RESULT_UNAVAILABLE,
    RESULT_UNWILLING_TO_PERFORM,
)
from ldap3.utils.conv import escape_filter_chars

from tests._fake_directory import USER_DN, FakeDirectory, alice_entry

_USER_DN = USER_DN


@pytest.fixture(autouse=True)
def configure_ldap(monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    monkeypatch.setattr(
        settings, "ldap_bind_dn", "CN=svc,OU=ServiceAccounts,DC=example,DC=com", raising=False
    )
    monkeypatch.setattr(settings, "ldap_bind_password", "svc-pw", raising=False)
    monkeypatch.setattr(settings, "ldap_user_base_dn", "OU=Users,DC=example,DC=com", raising=False)
    monkeypatch.setattr(settings, "ldap_user_filter", "(sAMAccountName={username})", raising=False)
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail", raising=False)
    monkeypatch.setattr(settings, "ldap_username_attribute", "sAMAccountName", raising=False)
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)


def _stub_search_found(monkeypatch, attrs=None):
    captured: dict = {}
    attrs = attrs or {
        settings.ldap_email_attribute: ["alice@example.com"],
        settings.ldap_username_attribute: ["alice"],
    }

    def fake_search(username):
        captured["username"] = username
        return _USER_DN, attrs

    monkeypatch.setattr(ldap_service, "_search_user", fake_search)
    return captured


@pytest.mark.asyncio
async def test_bind_user_success(monkeypatch):
    _stub_search_found(monkeypatch)
    monkeypatch.setattr(ldap_service, "_bind_as_user", lambda dn, pw: pw == "correct-horse")
    identity = await ldap_service.bind_user("alice", "correct-horse")
    assert identity is not None
    assert identity.email == "alice@example.com"
    assert identity.username == "alice"
    assert identity.dn == _USER_DN


@pytest.mark.asyncio
async def test_bind_user_wrong_password(monkeypatch):
    _stub_search_found(monkeypatch)
    monkeypatch.setattr(ldap_service, "_bind_as_user", lambda dn, pw: False)
    assert await ldap_service.bind_user("alice", "nope") is None


@pytest.mark.asyncio
async def test_bind_user_not_found(monkeypatch):
    monkeypatch.setattr(ldap_service, "_search_user", lambda username: None)
    called = {"bind": False}

    def fake_bind(dn, pw):
        called["bind"] = True
        return True

    monkeypatch.setattr(ldap_service, "_bind_as_user", fake_bind)
    assert await ldap_service.bind_user("bob", "correct-horse") is None
    assert called["bind"] is False


@pytest.mark.asyncio
async def test_bind_user_empty_password_rejected(monkeypatch):
    # Must short-circuit before touching the directory; anonymous bind would
    # otherwise accept an empty password.
    def boom(*_a, **_kw):
        raise AssertionError("should not be called")

    monkeypatch.setattr(ldap_service, "_search_user", boom)
    monkeypatch.setattr(ldap_service, "_bind_as_user", boom)
    assert await ldap_service.bind_user("alice", "") is None


@pytest.mark.asyncio
async def test_bind_user_missing_email_rejected(monkeypatch):
    _stub_search_found(
        monkeypatch,
        attrs={settings.ldap_email_attribute: [], settings.ldap_username_attribute: ["alice"]},
    )
    monkeypatch.setattr(ldap_service, "_bind_as_user", lambda dn, pw: True)
    assert await ldap_service.bind_user("alice", "correct-horse") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["ldap_server_url", "ldap_user_base_dn"])
async def test_bind_user_missing_config_raises_unavailable(monkeypatch, setting):
    """Issue #1403: incomplete configuration is the operator's problem, so it is
    "the directory could not be asked", never a refusal of the user's password."""

    def boom(*_a, **_kw):
        raise AssertionError("no directory call is made without a configuration")

    monkeypatch.setattr(ldap_service, "_search_user", boom)
    monkeypatch.setattr(ldap_service, "_bind_as_user", boom)
    monkeypatch.setattr(settings, setting, "", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.bind_user("alice", "correct-horse")


# --- Issue #1403: every failure class of a login, through the real client code
#     with only ldap3's Connection replaced (tests/_fake_directory.py). The
#     invariant: bind_user answers None only for what the directory said about
#     this login, and raises LdapUnavailableError whenever it could not be asked.

_PASSWORD = "correct-horse-battery"


@pytest.fixture
def directory(monkeypatch):
    fake = FakeDirectory(service_dn=settings.ldap_bind_dn)
    monkeypatch.setattr(ldap_service, "Connection", fake.connection)
    return fake


# (name, FakeDirectory overrides, settings overrides)
_UNAVAILABLE_CLASSES = {
    "service_connect_refused": (
        {"service_bind": LDAPSocketOpenError("socket connection error: Connection refused")},
        {},
    ),
    "service_connect_timeout": (
        {"service_bind": LDAPSocketOpenError("socket connection error: timed out")},
        {},
    ),
    "service_tls_failure": (
        {"service_start_tls": LDAPStartTLSError("wrap socket error: certificate verify failed")},
        {"ldap_use_tls": True},
    ),
    "service_bind_refused": ({"service_bind": RESULT_INVALID_CREDENTIALS}, {}),
    "anonymous_bind_refused": (
        {"service_bind": RESULT_INAPPROPRIATE_AUTHENTICATION},
        {"ldap_bind_dn": ""},
    ),
    "malformed_server_url": ({}, {"ldap_server_url": "ldap://mock:notaport"}),
    "search_transport_error": ({"search": LDAPSocketReceiveError("connection reset")}, {}),
    "search_slow_directory": ({"search": LDAPResponseTimeoutError("no response")}, {}),
    "search_bad_filter_config": ({"search": LDAPInvalidFilterError("malformed filter")}, {}),
    "search_base_dn_missing": ({"search": (RESULT_NO_SUCH_OBJECT, [])}, {}),
    "search_referral": ({"search": (RESULT_REFERRAL, [])}, {}),
    "search_busy": ({"search": (RESULT_BUSY, [])}, {}),
    "search_access_denied": ({"search": (RESULT_INSUFFICIENT_ACCESS_RIGHTS, [])}, {}),
    "user_bind_connect_error": (
        {"user_bind": LDAPSocketOpenError("socket connection error: Connection refused")},
        {},
    ),
    "user_bind_tls_failure": (
        {"user_start_tls": LDAPStartTLSError("wrap socket error: certificate verify failed")},
        {"ldap_use_tls": True},
    ),
    "user_bind_slow_directory": ({"user_bind": LDAPResponseTimeoutError("no response")}, {}),
    "user_bind_busy": ({"user_bind": RESULT_BUSY}, {}),
    "user_bind_unavailable": ({"user_bind": RESULT_UNAVAILABLE}, {}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,config", _UNAVAILABLE_CLASSES.values(), ids=_UNAVAILABLE_CLASSES.keys()
)
async def test_bind_user_raises_unavailable_when_the_directory_could_not_be_asked(
    monkeypatch, directory, overrides, config, caplog
):
    for name, value in config.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    if "ldap_bind_dn" in config:
        directory.service_dn = None
    for name, value in overrides.items():
        setattr(directory, name, value)
    with caplog.at_level("WARNING", logger="app.services.ldap_service"):
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.bind_user("alice", _PASSWORD)
    outage = [r for r in caplog.records if getattr(r, "action", None) == "ldap_error"]
    assert len(outage) == 1, [r.getMessage() for r in caplog.records]
    assert outage[0].levelname == "WARNING"
    for record in caplog.records:
        assert _PASSWORD not in record.getMessage()
        assert _PASSWORD not in str(record.__dict__)


# (name, FakeDirectory overrides). Every one is an answer the directory gave
# about THIS login, so it is the refusal (401), whatever the account's state:
# a disabled or locked account answers with the same codes a wrong password does
# on the directories HERD targets, and telling them apart would reveal that the
# account exists.
_REFUSAL_CLASSES = {
    "user_not_found": {"search": (RESULT_SUCCESS, [])},
    "wrong_password": {"user_bind": RESULT_INVALID_CREDENTIALS},
    "account_disabled_unwilling": {"user_bind": RESULT_UNWILLING_TO_PERFORM},
    "account_inappropriate_auth": {"user_bind": RESULT_INAPPROPRIATE_AUTHENTICATION},
    "account_locked_constraint": {"user_bind": RESULT_CONSTRAINT_VIOLATION},
    "account_access_denied": {"user_bind": RESULT_INSUFFICIENT_ACCESS_RIGHTS},
    "missing_email": {"search": (RESULT_SUCCESS, [alice_entry(mail=[])])},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", _REFUSAL_CLASSES.values(), ids=_REFUSAL_CLASSES.keys())
async def test_bind_user_answers_none_for_what_the_directory_said(directory, overrides, caplog):
    for name, value in overrides.items():
        setattr(directory, name, value)
    with caplog.at_level("INFO", logger="app.services.ldap_service"):
        assert await ldap_service.bind_user("alice", _PASSWORD) is None
    assert not [r for r in caplog.records if getattr(r, "action", None) == "ldap_error"]
    for record in caplog.records:
        assert _PASSWORD not in record.getMessage()


@pytest.mark.asyncio
async def test_bind_user_success_through_the_connection_seam(directory):
    identity = await ldap_service.bind_user("alice", _PASSWORD)
    assert identity == ldap_service.LdapIdentity(
        username="alice", email="alice@example.com", dn=USER_DN
    )
    assert directory.user_bind_passwords == [_PASSWORD]


@pytest.mark.asyncio
async def test_bind_user_unknown_user_never_binds_as_a_user(directory):
    directory.search = (RESULT_SUCCESS, [])
    assert await ldap_service.bind_user("alice", _PASSWORD) is None
    assert directory.user_bind_passwords == []


@pytest.mark.asyncio
async def test_bind_user_empty_password_opens_no_connection(directory):
    assert await ldap_service.bind_user("alice", "") is None
    assert directory.built == []


@pytest.mark.asyncio
async def test_user_bind_connection_has_the_receive_timeout(directory):
    """A directory that accepts the user bind's connection and never answers
    must end in the outage answer, not hold a worker thread forever."""
    await ldap_service.bind_user("alice", _PASSWORD)
    user_connections = [c for c in directory.built if c.get("user") == USER_DN]
    assert len(user_connections) == 1
    assert user_connections[0]["receive_timeout"] == ldap_service._RECEIVE_TIMEOUT_SECONDS


def test_filter_escaping_via_ldap3_conv():
    # Sanity check: the helper we rely on renders filter metacharacters safe.
    raw = "al*ice("
    escaped = escape_filter_chars(raw)
    assert "\\2a" in escaped
    assert "\\28" in escaped


class _FakeConnection:
    """Records the order of TLS/bind calls so the test can assert start_tls
    runs before bind. Search returns no entries so _search_user exits early."""

    def __init__(self, calls, *_a, **_kw):
        self._calls = calls
        self.entries = []
        self.result = None

    def start_tls(self):
        self._calls.append("start_tls")
        return True

    def bind(self):
        self._calls.append("bind")
        return True

    def search(self, *_a, **_kw):
        # A successful search with no match: the directory's "no such user".
        self.result = {"result": RESULT_SUCCESS}
        return False

    def unbind(self):
        return True


def _patch_connection(monkeypatch, calls):
    def factory(*args, **kwargs):
        return _FakeConnection(calls, *args, **kwargs)

    monkeypatch.setattr(ldap_service, "Connection", factory)
    monkeypatch.setattr(ldap_service, "_build_server", lambda: object())


def test_search_user_starts_tls_before_bind(monkeypatch):
    # Plain ldap:// with ldap_use_tls=True (the default) must negotiate
    # StartTLS before binding, or the service-account credentials cross the
    # wire in plaintext.
    monkeypatch.setattr(settings, "ldap_use_tls", True, raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    calls: list[str] = []
    _patch_connection(monkeypatch, calls)

    ldap_service._search_user("alice")

    assert "start_tls" in calls
    assert "bind" in calls
    assert calls.index("start_tls") < calls.index("bind")


def test_bind_as_user_starts_tls_before_bind(monkeypatch):
    # Same ordering guarantee for the user-password bind connection.
    monkeypatch.setattr(settings, "ldap_use_tls", True, raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    calls: list[str] = []
    _patch_connection(monkeypatch, calls)

    assert ldap_service._bind_as_user(_USER_DN, "correct-horse") is True

    assert calls.index("start_tls") < calls.index("bind")


def test_no_start_tls_when_disabled(monkeypatch):
    # ldap_use_tls=False (the suite default) must not attempt StartTLS.
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    calls: list[str] = []
    _patch_connection(monkeypatch, calls)

    ldap_service._search_user("alice")
    ldap_service._bind_as_user(_USER_DN, "correct-horse")

    assert "start_tls" not in calls


def test_no_start_tls_for_ldaps_url(monkeypatch):
    # ldaps:// already implies TLS at connect; a redundant StartTLS would error.
    monkeypatch.setattr(settings, "ldap_use_tls", True, raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldaps://mock", raising=False)
    calls: list[str] = []
    _patch_connection(monkeypatch, calls)

    ldap_service._search_user("alice")
    ldap_service._bind_as_user(_USER_DN, "correct-horse")

    assert "start_tls" not in calls


# --- TLS certificate validation (the bind transmits passwords; the cert must
#     be verified by default to prevent MITM credential capture) ---


def test_build_tls_validates_certificate_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ldap_tls_validate", True, raising=False)
    monkeypatch.setattr(settings, "ldap_ca_cert", "", raising=False)
    tls = ldap_service._build_tls()
    assert tls.validate == ssl.CERT_REQUIRED


def test_build_tls_uses_ca_cert_when_set(monkeypatch, tmp_path):
    # ldap3.Tls validates that the CA file exists at construction, so point at a
    # real file. A missing path failing loudly is itself desirable behavior.
    ca_file = tmp_path / "ldap-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(settings, "ldap_tls_validate", True, raising=False)
    monkeypatch.setattr(settings, "ldap_ca_cert", str(ca_file), raising=False)
    tls = ldap_service._build_tls()
    assert tls.validate == ssl.CERT_REQUIRED
    assert tls.ca_certs_file == str(ca_file)


def test_build_tls_can_opt_out_of_validation(monkeypatch):
    monkeypatch.setattr(settings, "ldap_tls_validate", False, raising=False)
    tls = ldap_service._build_tls()
    assert tls.validate == ssl.CERT_NONE


def test_build_server_attaches_validating_tls_when_tls_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ldap_use_tls", True, raising=False)
    monkeypatch.setattr(settings, "ldap_tls_validate", True, raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldaps://mock", raising=False)
    server = ldap_service._build_server()
    assert server.tls is not None
    assert server.tls.validate == ssl.CERT_REQUIRED


# --- _fetch_group_sync branches the live directory cannot express: the LDIF
#     fixtures use groupOfNames, whose schema makes cn and member mandatory,
#     so the missing-attribute branches are pinned here with a stubbed entry
#     (AD models an empty group as an entry with no member attribute) ---

_GROUP_DN = "CN=eng,OU=Groups,DC=example,DC=com"


class _FakeAttribute:
    def __init__(self, values):
        self.values = values


class _FakeEntry:
    def __init__(self, dn, attrs):
        self.entry_dn = dn
        self._attrs = attrs

    def __contains__(self, attr):
        return attr in self._attrs

    def __getitem__(self, attr):
        return _FakeAttribute(self._attrs[attr])


def _patch_group_fetch(monkeypatch, entry):
    @contextmanager
    def fake_connection(require_service_account=False):
        yield object()

    monkeypatch.setattr(ldap_service, "_service_connection", fake_connection)
    monkeypatch.setattr(ldap_service, "_base_entry", lambda conn, dn, attrs: entry)


def test_fetch_group_missing_name_attribute_falls_back_to_dn(monkeypatch):
    # The cached directory_name must never be empty; a group entry without
    # its display-name attribute keeps the DN as its name.
    entry = _FakeEntry(_GROUP_DN, {"member": ["uid=a,ou=people,dc=example,dc=com"]})
    _patch_group_fetch(monkeypatch, entry)
    group = ldap_service._fetch_group_sync(_GROUP_DN)
    assert group is not None
    assert group.name == _GROUP_DN
    assert group.member_dns == ("uid=a,ou=people,dc=example,dc=com",)


def test_fetch_group_missing_member_attribute_is_empty_membership(monkeypatch):
    # An entry lacking the member attribute is an empty group (the AD empty
    # group shape), not an error: the reconciler must receive an empty
    # desired set, never a KeyError or LdapUnavailableError.
    entry = _FakeEntry(_GROUP_DN, {"cn": ["eng"]})
    _patch_group_fetch(monkeypatch, entry)
    group = ldap_service._fetch_group_sync(_GROUP_DN)
    assert group is not None
    assert group.name == "eng"
    assert group.member_dns == ()


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: present_emails/disabled_emails paged enumeration. The
# load-bearing rule under test: a partial or errored page must never be
# mistaken for a complete enumeration.
# ---------------------------------------------------------------------------


class _FakePagedConnection:
    """Simulates ldap3's paged search across N scripted pages.

    pages: a list of (entries, result_code, cookie) tuples consumed in
    order, one per search() call. cookie is bytes for "more pages follow";
    an empty/None cookie means the directory reports it exhausted; omitting
    the controls key entirely (cookie=NO_CONTROL) simulates a server that
    drops the paging control on its final page.
    """

    NO_CONTROL = object()

    def __init__(self, pages):
        self._pages = list(pages)
        self.entries: list = []
        self.result: dict = {}
        self.search_filters: list[str] = []
        self.paged_cookies: list[bytes | None] = []

    def search(
        self, *, search_base, search_filter, search_scope, attributes, paged_cookie=None, **_kw
    ):
        self.search_filters.append(search_filter)
        self.paged_cookies.append(paged_cookie)
        entries, result_code, cookie = self._pages.pop(0)
        self.entries = entries
        if result_code != RESULT_SUCCESS:
            self.result = {"result": result_code}
            return False
        controls = {}
        if cookie is not self.NO_CONTROL:
            controls = {ldap_service._PAGED_COOKIE_OID: {"value": {"cookie": cookie or b""}}}
        self.result = {"result": RESULT_SUCCESS, "controls": controls}
        return True


class _RaisingConnection:
    def search(self, **_kw):
        raise LDAPException("connection reset")


def _patch_paged_connection(monkeypatch, fake_conn):
    @contextmanager
    def fake_connection(require_service_account=False):
        yield fake_conn

    monkeypatch.setattr(ldap_service, "_service_connection", fake_connection)


def _mail_entry(dn: str, email: str) -> _FakeEntry:
    return _FakeEntry(dn, {settings.ldap_email_attribute: [email]})


def test_present_emails_collects_across_multiple_pages_lowercased(monkeypatch):
    fake_conn = _FakePagedConnection(
        [
            ([_mail_entry("uid=a", "A@example.com")], RESULT_SUCCESS, b"cookie-1"),
            ([_mail_entry("uid=b", "b@example.com")], RESULT_SUCCESS, b""),
        ]
    )
    _patch_paged_connection(monkeypatch, fake_conn)

    emails = ldap_service._present_emails_sync()

    assert emails == frozenset({"a@example.com", "b@example.com"})
    # Two pages consumed: the first call sent no cookie and the second carried
    # the first page's cookie (a loop that restarted from page one would
    # never reach the second page on a real server).
    assert fake_conn.paged_cookies == [None, b"cookie-1"]


def test_present_emails_collects_multivalued_attribute(monkeypatch):
    entry = _FakeEntry("uid=multi", {settings.ldap_email_attribute: ["a@x.com", "b@x.com"]})
    fake_conn = _FakePagedConnection([([entry], RESULT_SUCCESS, b"")])
    _patch_paged_connection(monkeypatch, fake_conn)

    assert ldap_service._present_emails_sync() == frozenset({"a@x.com", "b@x.com"})


def test_present_emails_missing_control_after_success_ends_pages(monkeypatch):
    # A server that omits the paging control entirely on its only/last page:
    # end-of-pages is inferred ONLY because the search already proved
    # success, per the module contract.
    fake_conn = _FakePagedConnection(
        [([_mail_entry("uid=a", "a@x.com")], RESULT_SUCCESS, _FakePagedConnection.NO_CONTROL)]
    )
    _patch_paged_connection(monkeypatch, fake_conn)

    assert ldap_service._present_emails_sync() == frozenset({"a@x.com"})


def test_present_emails_raises_on_non_success_page(monkeypatch):
    # A page mid-enumeration reports a non-success result: the whole
    # enumeration must raise, never return the partial set collected so far.
    fake_conn = _FakePagedConnection(
        [
            ([_mail_entry("uid=a", "a@x.com")], RESULT_SUCCESS, b"cookie-1"),
            ([], RESULT_NO_SUCH_OBJECT, None),
        ]
    )
    _patch_paged_connection(monkeypatch, fake_conn)

    with pytest.raises(ldap_service.LdapUnavailableError):
        ldap_service._present_emails_sync()


def test_present_emails_raises_on_ldap_exception(monkeypatch):
    _patch_paged_connection(monkeypatch, _RaisingConnection())

    with pytest.raises(ldap_service.LdapUnavailableError):
        ldap_service._present_emails_sync()


def test_disabled_emails_conjoins_filter_with_presence(monkeypatch):
    monkeypatch.setattr(settings, "ldap_disabled_filter", "(pwdAccountLockedTime=*)", raising=False)
    fake_conn = _FakePagedConnection([([], RESULT_SUCCESS, b"")])
    _patch_paged_connection(monkeypatch, fake_conn)

    result = ldap_service._disabled_emails_sync()

    assert result == frozenset()
    assert fake_conn.search_filters == [
        f"(&({settings.ldap_email_attribute}=*)(pwdAccountLockedTime=*))"
    ]


@pytest.mark.asyncio
async def test_disabled_emails_empty_filter_raises_value_error(monkeypatch):
    monkeypatch.setattr(settings, "ldap_disabled_filter", "", raising=False)

    with pytest.raises(ValueError):
        await ldap_service.disabled_emails()


@pytest.mark.asyncio
async def test_disabled_emails_filter_not_starting_with_paren_raises_value_error(monkeypatch):
    monkeypatch.setattr(
        settings, "ldap_disabled_filter", "userAccountControl:1.2:=2", raising=False
    )

    with pytest.raises(ValueError):
        await ldap_service.disabled_emails()
