"""A real ldap3 directory in memory (`MOCK_SYNC`), for input-dependent login tests.

Unlike tests/_fake_directory.py, every connection here is a real ldap3
`Connection`, so everything ldap3 does on the client runs: building the filter,
SASLprep of the simple-bind password (`validate_simple_password` inside `bind()`),
the BER encoding, and decoding the search response. Use it wherever the answer
depends on the submitted username or password (issue #1403 review: a password
ldap3 refuses client-side must get the same answer for every username).

`ldap_service.Connection` is replaced by `MockDirectory.connection`, which builds
a `MOCK_SYNC` connection over ONE shared entry store, so the service-account
search and the user bind see the same directory. `connections` records the
`user` of every connection ldap_service opened, so a test can prove that no
directory call was made, and `binds` the `user` of every `bind()` called on one,
so a test can prove that no password was tried.

ldap3's `MOCK_SYNC` answers a search with a size limit with at most that many
entries and success, never sizeLimitExceeded, on a connection without
`raise_exceptions` (ldap_service never sets it): a directory that truncates
silently. tests/_fake_directory.py scripts that result code.
"""

from __future__ import annotations

from ldap3 import MOCK_SYNC, Connection, Server

BASE_DN = "ou=people,dc=example,dc=com"
SERVICE_DN = "cn=svc,dc=example,dc=com"
SERVICE_PASSWORD = "svc-pw"


class MockDirectory:
    def __init__(self) -> None:
        self.server = Server("mock-directory")
        seed = Connection(self.server, client_strategy=MOCK_SYNC)
        self._entries = seed.strategy.entries
        seed.strategy.add_entry(
            SERVICE_DN, {"userPassword": SERVICE_PASSWORD, "objectClass": "person"}
        )
        seed.strategy.add_entry(BASE_DN, {"objectClass": "organizationalUnit"})
        self._seed = seed
        self.connections: list[str | None] = []
        self.binds: list[str | None] = []

    def add_user(self, uid: str, password: str, **attributes) -> str:
        """A person under BASE_DN with uid, mail `<uid>@example.com`, and the
        password; `attributes` replace or add attributes, and a value of None
        leaves that attribute out. A `bytes` value is stored as those octets, so
        ldap3 decodes it the way it decodes a directory's answer (a value that is
        not UTF-8 comes back as `bytes`)."""
        dn = f"uid={uid},{BASE_DN}"
        values = {
            "uid": uid,
            "mail": f"{uid}@example.com",
            "userPassword": password,
            "objectClass": "person",
            **attributes,
        }
        self.add_entry(dn, {k: v for k, v in values.items() if v is not None})
        return dn

    def add_entry(self, dn: str, attributes: dict) -> str:
        """Any entry, such as a group (`member` holding member DNs)."""
        self._seed.strategy.add_entry(dn, attributes)
        return dn

    def connection(self, _server, **kwargs) -> Connection:
        self.connections.append(kwargs.get("user"))
        conn = Connection(self.server, client_strategy=MOCK_SYNC, **kwargs)
        conn.strategy.entries = self._entries
        bind = conn.bind

        def counted_bind(*args, **bind_kwargs):
            self.binds.append(kwargs.get("user"))
            return bind(*args, **bind_kwargs)

        conn.bind = counted_bind
        return conn


def configure(monkeypatch, settings, ldap_service) -> MockDirectory:
    """LDAP mode pointed at a fresh MockDirectory; returns it."""
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock-directory", raising=False)
    monkeypatch.setattr(settings, "ldap_bind_dn", SERVICE_DN, raising=False)
    monkeypatch.setattr(settings, "ldap_bind_password", SERVICE_PASSWORD, raising=False)
    monkeypatch.setattr(settings, "ldap_user_base_dn", BASE_DN, raising=False)
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username})", raising=False)
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail", raising=False)
    monkeypatch.setattr(settings, "ldap_username_attribute", "uid", raising=False)
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)
    directory = MockDirectory()
    monkeypatch.setattr(ldap_service, "Connection", directory.connection)
    return directory
