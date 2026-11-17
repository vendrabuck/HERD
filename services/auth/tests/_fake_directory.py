"""A scripted stand-in for ldap3's `Connection`, for the LDAP login path (issue #1403).

`ldap_service` builds every connection through its module attribute `Connection`
(and `Server`), so a test replaces that attribute with `FakeDirectory.connection`
and scripts what the directory does in each phase of a login:

- the service connection (the one whose `user` is the configured bind DN, or
  None for an anonymous bind): StartTLS, the bind, then the user search;
- the user connection (any other `user`): StartTLS, then the bind with the
  submitted password.

Each phase is scripted as an outcome, never as a classification: a bind is a
result code (0 is success) or an exception to raise, and a search is a result
code plus entries or an exception. Whether an outcome means "refused" or "could
not be asked" is decided by `ldap_service` alone; this fake only plays the
directory, so it cannot implement the behavior under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ldap3.core.results import RESULT_SUCCESS

# The DN `_search_user` finds and the user bind is attempted as.
USER_DN = "CN=alice,OU=Users,DC=example,DC=com"


class _Attribute:
    def __init__(self, values: list[str]) -> None:
        self.values = values


class FakeEntry:
    """An ldap3 Entry as `_search_user` reads it: `entry_dn`, `in`, and `[attr].values`."""

    def __init__(self, dn: str, attrs: dict[str, list[str]]) -> None:
        self.entry_dn = dn
        self._attrs = attrs

    def __contains__(self, attr: str) -> bool:
        return attr in self._attrs

    def __getitem__(self, attr: str) -> _Attribute:
        return _Attribute(self._attrs[attr])


def alice_entry(mail: list[str] | None = None) -> FakeEntry:
    return FakeEntry(
        USER_DN,
        {"mail": ["alice@example.com"] if mail is None else mail, "sAMAccountName": ["alice"]},
    )


@dataclass
class FakeDirectory:
    """What the directory does in each phase; the defaults are a successful login."""

    service_dn: str | None
    # A result code (0 binds), or an exception instance raised by bind().
    service_bind: int | BaseException = RESULT_SUCCESS
    # Raised by start_tls() on the service connection, when set.
    service_start_tls: BaseException | None = None
    # (result code, entries), or an exception instance raised by search().
    search: tuple[int, list[FakeEntry]] | BaseException = field(
        default_factory=lambda: (RESULT_SUCCESS, [alice_entry()])
    )
    # A result code (0 binds), or an exception instance raised by bind().
    user_bind: int | BaseException = RESULT_SUCCESS
    # Raised by start_tls() on the user connection, when set.
    user_start_tls: BaseException | None = None
    # Every connection built, in order, with the keyword arguments it was built with.
    built: list[dict] = field(default_factory=list)
    # Every password a user bind was attempted with.
    user_bind_passwords: list[str] = field(default_factory=list)

    def connection(self, _server, **kwargs) -> _FakeConnection:
        self.built.append(kwargs)
        return _FakeConnection(self, kwargs)


class _FakeConnection:
    def __init__(self, directory: FakeDirectory, kwargs: dict) -> None:
        self._directory = directory
        self._user = kwargs.get("user")
        self._password = kwargs.get("password")
        self._is_service = self._user == directory.service_dn
        self.result: dict | None = None
        self.entries: list[FakeEntry] = []
        self.socket = None

    def start_tls(self) -> bool:
        error = (
            self._directory.service_start_tls
            if self._is_service
            else self._directory.user_start_tls
        )
        if error is not None:
            raise error
        return True

    def bind(self) -> bool:
        if not self._is_service:
            self._directory.user_bind_passwords.append(self._password)
        outcome = self._directory.service_bind if self._is_service else self._directory.user_bind
        if isinstance(outcome, BaseException):
            raise outcome
        self.result = {"result": outcome, "description": f"code {outcome}"}
        return outcome == RESULT_SUCCESS

    def search(self, **_kwargs) -> bool:
        outcome = self._directory.search
        if isinstance(outcome, BaseException):
            raise outcome
        code, entries = outcome
        self.result = {"result": code, "description": f"code {code}"}
        self.entries = list(entries)
        return code == RESULT_SUCCESS and bool(entries)

    def unbind(self) -> bool:
        return True
