"""Unit tests for ldap_service.bind_user.

These tests stub the two internal helpers (_search_user, _bind_as_user) so we
can assert the orchestration + filter escaping without spinning up a real
ldap3 mock directory. The thin wrappers around ldap3 Connection are exercised
in the integration stack, not here.
"""

import ssl
from contextlib import contextmanager

import pytest
from app.config import settings
from app.services import ldap_service
from ldap3.core.exceptions import LDAPException
from ldap3.core.results import RESULT_NO_SUCH_OBJECT, RESULT_SUCCESS
from ldap3.utils.conv import escape_filter_chars

_USER_DN = "CN=alice,OU=Users,DC=example,DC=com"


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
async def test_bind_user_missing_config_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "ldap_server_url", "", raising=False)
    assert await ldap_service.bind_user("alice", "correct-horse") is None


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

    def start_tls(self):
        self._calls.append("start_tls")
        return True

    def bind(self):
        self._calls.append("bind")
        return True

    def search(self, *_a, **_kw):
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

    def search(
        self, *, search_base, search_filter, search_scope, attributes, paged_cookie=None, **_kw
    ):
        self.search_filters.append(search_filter)
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
    # Two pages consumed; the second call carried the first page's cookie.
    assert len(fake_conn.search_filters) == 2


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
