"""Unit tests for ldap_service.run_connection (issue #513 items 2, 3, 4, 10).

These drive the REAL fetch_group/resolve_members/_open_service_connection
code paths against a fake ldap3 Connection/Server (never a real socket), so
the assertions are actual connect/bind counts, not a proxy for them. The
directory-answer classification (fetch_group returning None, a member
resolving with a skip_reason) is exercised elsewhere (test_ldap_service.py,
test_ldap_sync_service.py); this file is scoped to connection sharing,
count, and the retry-scope behavior.

run_connection() yields an EXPLICIT holder (item 2): there is no module
global to guard reentrancy against, so two holders (nested or concurrent)
are simply independent objects, and a caller that never receives a holder
(the common case: any call that omits run_holder) always gets a private
connection regardless of what else is going on in the process.
"""

import asyncio

import pytest
from app.config import settings
from app.services import ldap_service
from ldap3.core.exceptions import LDAPException, LDAPSocketReceiveError
from ldap3.core.results import RESULT_NO_SUCH_OBJECT, RESULT_OPERATIONS_ERROR, RESULT_SUCCESS

_PEOPLE = "ou=people,dc=company,dc=local"
_GROUP_DN = "cn=herd-eng,ou=groups,dc=company,dc=local"


@pytest.fixture(autouse=True)
def configure_ldap(monkeypatch):
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://mock", raising=False)
    monkeypatch.setattr(
        settings, "ldap_bind_dn", "CN=svc,OU=ServiceAccounts,DC=company,DC=local", raising=False
    )
    monkeypatch.setattr(settings, "ldap_bind_password", "svc-pw", raising=False)
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)
    monkeypatch.setattr(settings, "ldap_group_name_attribute", "cn", raising=False)
    monkeypatch.setattr(settings, "ldap_group_member_attribute", "member", raising=False)
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail", raising=False)
    monkeypatch.setattr(settings, "ldap_username_attribute", "sAMAccountName", raising=False)


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


def _group_dn(n: int) -> str:
    return f"cn=herd-group{n},ou=groups,dc=company,dc=local"


def _member_dn(n: int) -> str:
    return f"uid=user{n},{_PEOPLE}"


class _FakeConnection:
    """Minimal ldap3 Connection double for BASE-scope entry lookups.

    entries_by_dn maps a DN to an {attr: [values]} dict; a DN absent from
    it is a proven-absent BASE search (RESULT_NO_SUCH_OBJECT), matching
    _base_entry's contract.

    bind_ok controls whether bind() succeeds. raise_on_search_calls maps a
    1-indexed search call number (counted across this connection instance's
    own lifetime) to the ldap3 exception CLASS to raise on that call,
    simulating a connection drop; bad_result_on_search_calls is a
    frozenset of call numbers that instead return a non-benign result code
    with NO exception at all (a directory-side error, not a transport
    failure) so _checked_search's final unconditional raise fires with no
    __cause__.
    """

    def __init__(
        self,
        entries_by_dn,
        *,
        bind_ok=True,
        raise_on_search_calls=None,
        bad_result_on_search_calls=frozenset(),
    ):
        self.entries_by_dn = entries_by_dn
        self.bind_ok = bind_ok
        self.raise_on_search_calls = raise_on_search_calls or {}
        self.bad_result_on_search_calls = bad_result_on_search_calls
        self.entries: list = []
        self.result: dict = {}
        self.search_calls = 0
        self.bind_calls = 0
        self.unbind_calls = 0

    def start_tls(self):
        return True

    def bind(self):
        self.bind_calls += 1
        return self.bind_ok

    def search(self, *, search_base, search_filter, search_scope, attributes, **_kw):
        self.search_calls += 1
        if self.search_calls in self.raise_on_search_calls:
            raise self.raise_on_search_calls[self.search_calls]("connection reset")
        if self.search_calls in self.bad_result_on_search_calls:
            self.entries = []
            self.result = {"result": RESULT_OPERATIONS_ERROR}
            return False
        entry = self.entries_by_dn.get(search_base)
        if entry is None:
            self.entries = []
            self.result = {"result": RESULT_NO_SUCH_OBJECT}
            return False
        self.entries = [_FakeEntry(search_base, entry)]
        self.result = {"result": RESULT_SUCCESS}
        return True

    def unbind(self):
        self.unbind_calls += 1
        return True


def _group_entry(member_dns):
    return {"cn": ["herd-eng"], "member": list(member_dns)}


def _member_entry(n):
    return {"mail": [f"user{n}@company.local"], "sAMAccountName": [f"user{n}"]}


def _patch_connection_factory(monkeypatch, connections):
    """Patch ldap_service.Connection to hand out connections from the given
    list in order, one per construction; also patches _build_server to skip
    real network setup. Returns the list of constructed connections in
    construction order."""
    constructed: list = []
    remaining = list(connections)

    def factory(*_a, **_kw):
        conn = remaining.pop(0)
        constructed.append(conn)
        return conn

    monkeypatch.setattr(ldap_service, "Connection", factory)
    monkeypatch.setattr(ldap_service, "_build_server", lambda: object())
    return constructed


# ---------------------------------------------------------------------------
# Connection-count: shared vs. private, for a multi-mapping run shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_run_connection_one_bind_for_three_mappings(monkeypatch):
    # Three "mappings" worth of fetch_group + resolve_members (2 calls each,
    # 6 total) inside ONE run_connection() context, all passing the SAME
    # explicit holder (issue #513 item 2: never a module global).
    entries = {}
    for n in (1, 2, 3):
        entries[_group_dn(n)] = _group_entry([_member_dn(n)])
        entries[_member_dn(n)] = _member_entry(n)
    conn = _FakeConnection(entries)
    constructed = _patch_connection_factory(monkeypatch, [conn])

    async with ldap_service.run_connection() as holder:
        for n in (1, 2, 3):
            entry = await ldap_service.fetch_group(_group_dn(n), run_holder=holder)
            assert entry is not None
            resolutions = await ldap_service.resolve_members(entry.member_dns, run_holder=holder)
            assert resolutions[0].identity is not None
            assert resolutions[0].identity.email == f"user{n}@company.local"

    # One Connection() construction, one bind, for the WHOLE run: 6 calls
    # collapsed to a single connect+TLS+bind cycle (was 2 per mapping = 6).
    assert len(constructed) == 1
    assert conn.bind_calls == 1
    assert conn.unbind_calls == 1
    assert conn.search_calls == 6


@pytest.mark.asyncio
async def test_without_run_connection_each_call_opens_its_own(monkeypatch):
    # The SAME 6 calls made with NO holder passed at all: private,
    # per-call connections, exactly the pre-item-3 behavior (2N: one for
    # fetch_group, one for resolve_members, per mapping).
    entries = {}
    for n in (1, 2, 3):
        entries[_group_dn(n)] = _group_entry([_member_dn(n)])
        entries[_member_dn(n)] = _member_entry(n)
    conns = [_FakeConnection(dict(entries)) for _ in range(6)]
    constructed = _patch_connection_factory(monkeypatch, conns)

    for n in (1, 2, 3):
        entry = await ldap_service.fetch_group(_group_dn(n))
        assert entry is not None
        resolutions = await ldap_service.resolve_members(entry.member_dns)
        assert resolutions[0].identity is not None

    assert len(constructed) == 6
    assert all(c.bind_calls == 1 for c in constructed)
    assert all(c.unbind_calls == 1 for c in constructed)


# ---------------------------------------------------------------------------
# Retry scope (issue #513 item 4): retry once ONLY when the failing op ran
# on an ALREADY-LIVE connection AND the cause is a transport-class error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_connection_reconnects_once_and_succeeds(monkeypatch):
    entries = {_group_dn(1): _group_entry([_member_dn(1)]), _member_dn(1): _member_entry(1)}
    # The first connection's search #1 (fetch_group's) succeeds, so the
    # connection is LIVE by the time search #2 (resolve_members') hits a
    # transport-class failure (LDAPSocketReceiveError, an
    # LDAPCommunicationError subclass): eligible for one retry.
    first = _FakeConnection(entries, raise_on_search_calls={2: LDAPSocketReceiveError})
    second = _FakeConnection(entries)
    constructed = _patch_connection_factory(monkeypatch, [first, second])

    async with ldap_service.run_connection() as holder:
        entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
        assert entry is not None
        resolutions = await ldap_service.resolve_members(entry.member_dns, run_holder=holder)
        assert resolutions[0].identity is not None
        assert resolutions[0].identity.email == "user1@company.local"

    assert len(constructed) == 2
    assert first.bind_calls == 1
    assert first.search_calls == 2
    assert first.unbind_calls == 1
    assert second.bind_calls == 1
    assert second.search_calls == 1


@pytest.mark.asyncio
async def test_persistent_transport_failure_raises_after_exactly_one_retry(monkeypatch):
    entries = {_group_dn(1): _group_entry([_member_dn(1)]), _member_dn(1): _member_entry(1)}
    # first becomes live via a successful search #1, then fails
    # transport-class on search #2; the reconnected second ALSO fails
    # transport-class on its very first search. Must still raise
    # LdapUnavailableError (not a novel failure mode) after exactly one
    # retry, never an unbounded loop.
    first = _FakeConnection(entries, raise_on_search_calls={2: LDAPSocketReceiveError})
    second = _FakeConnection(entries, raise_on_search_calls={1: LDAPSocketReceiveError})
    constructed = _patch_connection_factory(monkeypatch, [first, second])

    async with ldap_service.run_connection() as holder:
        entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
        assert entry is not None
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.resolve_members(entry.member_dns, run_holder=holder)

    assert len(constructed) == 2
    assert first.search_calls == 2
    assert second.search_calls == 1


@pytest.mark.asyncio
async def test_non_benign_result_code_on_live_connection_does_not_retry(monkeypatch):
    # The connection IS already live (search #1 succeeds), but search #2
    # fails with a non-benign directory RESULT CODE, not an exception: no
    # __cause__, so _is_transport_failure is False and no retry is
    # attempted, exactly matching "a permanent result-code error does not
    # reconnect" (issue #513 item 4).
    entries = {_group_dn(1): _group_entry([_member_dn(1)]), _member_dn(1): _member_entry(1)}
    conn = _FakeConnection(entries, bad_result_on_search_calls={2})
    constructed = _patch_connection_factory(monkeypatch, [conn])

    async with ldap_service.run_connection() as holder:
        entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
        assert entry is not None
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.resolve_members(entry.member_dns, run_holder=holder)

    assert len(constructed) == 1
    assert conn.search_calls == 2


@pytest.mark.asyncio
async def test_non_transport_ldap_exception_on_live_connection_does_not_retry(monkeypatch):
    # A live connection whose second search raises a plain, non-transport
    # LDAPException (e.g. a malformed filter): _is_transport_failure is
    # False (not an LDAPCommunicationError), so no retry either.
    entries = {_group_dn(1): _group_entry([_member_dn(1)]), _member_dn(1): _member_entry(1)}
    conn = _FakeConnection(entries, raise_on_search_calls={2: LDAPException})
    constructed = _patch_connection_factory(monkeypatch, [conn])

    async with ldap_service.run_connection() as holder:
        entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
        assert entry is not None
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.resolve_members(entry.member_dns, run_holder=holder)

    assert len(constructed) == 1
    assert conn.search_calls == 2


@pytest.mark.asyncio
async def test_initial_bind_failure_attempts_exactly_one_connect_no_retry(monkeypatch):
    # The connection was NEVER live (this is the very first op on it): a
    # bind failure here must not retry at all, even though bind failures
    # raise LdapUnavailableError just like a transport failure would (issue
    # #513 item 4: "never on the initial open/bind failure").
    conn = _FakeConnection({}, bind_ok=False)
    constructed = _patch_connection_factory(monkeypatch, [conn])

    async with ldap_service.run_connection() as holder:
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.fetch_group(_group_dn(1), run_holder=holder)

    assert len(constructed) == 1
    assert conn.bind_calls == 1
    # issue #513 item 8: the failed-bind connection is still closed.
    assert conn.unbind_calls == 1


# ---------------------------------------------------------------------------
# Explicit threading (issue #513 item 2): a holder is never shared unless
# the SAME object reference is passed; an unrelated concurrent caller that
# omits run_holder always gets its own private connection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_caller_without_holder_never_touches_the_run_holder(monkeypatch):
    # Both fakes serve the UNION of entries: asyncio.gather does not fix
    # which task reaches the connection factory first, so under a slower
    # scheduler (CI coverage tracer) the outside caller can be handed the
    # first fake. Whichever fake each side receives must resolve its group,
    # or the assert below misfires on ordering rather than on the invariant
    # under test (two separate connections, never merged).
    all_entries = {
        _group_dn(1): _group_entry([_member_dn(1)]),
        _member_dn(1): _member_entry(1),
        _group_dn(2): _group_entry([_member_dn(2)]),
    }
    conn_a = _FakeConnection(dict(all_entries))
    conn_b = _FakeConnection(dict(all_entries))
    constructed = _patch_connection_factory(monkeypatch, [conn_a, conn_b])

    async def run_side():
        async with ldap_service.run_connection() as holder:
            entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
            assert entry is not None
            await ldap_service.resolve_members(entry.member_dns, run_holder=holder)
            # Give the concurrent outside caller a chance to interleave
            # while this run's holder is still open.
            await asyncio.sleep(0)

    async def outside_caller():
        # A router-style call (e.g. mapping validation) that never receives
        # any holder: must open its OWN private connection, never the
        # in-flight run's.
        await asyncio.sleep(0)
        entry = await ldap_service.fetch_group(_group_dn(2))
        assert entry is not None

    await asyncio.gather(run_side(), outside_caller())

    # Two SEPARATE connections: the run's shared one (used twice) and the
    # outside caller's own private one (used once), never merged. Which fake
    # played which role depends on scheduling, so assert on the multiset of
    # per-connection call counts rather than on a fixed assignment.
    assert len(constructed) == 2
    assert conn_a.bind_calls == 1
    assert conn_b.bind_calls == 1
    assert sorted([conn_a.search_calls, conn_b.search_calls]) == [1, 2]


@pytest.mark.asyncio
async def test_nested_run_connection_holders_are_independent(monkeypatch):
    # No module global to guard: nesting is structurally safe, and each
    # holder owns its own connection.
    outer_entries = {_group_dn(1): _group_entry([_member_dn(1)])}
    inner_entries = {_group_dn(2): _group_entry([_member_dn(2)])}
    outer_conn = _FakeConnection(outer_entries)
    inner_conn = _FakeConnection(inner_entries)
    constructed = _patch_connection_factory(monkeypatch, [outer_conn, inner_conn])

    async with ldap_service.run_connection() as outer_holder:
        await ldap_service.fetch_group(_group_dn(1), run_holder=outer_holder)
        async with ldap_service.run_connection() as inner_holder:
            await ldap_service.fetch_group(_group_dn(2), run_holder=inner_holder)
        # Inner holder closed; outer holder's connection is untouched.
        assert inner_conn.unbind_calls == 1
        assert outer_conn.unbind_calls == 0

    assert len(constructed) == 2
    assert outer_conn.search_calls == 1
    assert inner_conn.search_calls == 1


# ---------------------------------------------------------------------------
# Item 10: a holder that never opened a connection costs nothing to close.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_connection_with_no_calls_opens_nothing(monkeypatch):
    def factory(*_a, **_kw):
        raise AssertionError("Connection() should never be constructed")

    monkeypatch.setattr(ldap_service, "Connection", factory)
    monkeypatch.setattr(ldap_service, "_build_server", lambda: object())

    async with ldap_service.run_connection():
        pass  # no fetch_group/resolve_members calls at all


@pytest.mark.asyncio
async def test_holder_closed_on_exit_even_after_exception(monkeypatch):
    entries = {_group_dn(1): _group_entry([_member_dn(1)])}
    conn = _FakeConnection(entries)
    _patch_connection_factory(monkeypatch, [conn])

    with pytest.raises(RuntimeError):
        async with ldap_service.run_connection() as holder:
            await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
            raise RuntimeError("boom")

    assert conn.unbind_calls == 1


# ---------------------------------------------------------------------------
# Item 7: a run with mappings AND an enabled deactivation sweep still pays
# only ONE connect+TLS+bind cycle, end to end through
# ldap_sync_service.execute_run (not just the primitive-level tests above).
# ---------------------------------------------------------------------------


class _FakeGroupSyncConnection:
    """Serves the three search shapes one real reconciler run issues on a
    shared connection: a BASE-scope group entry fetch, BASE-scope member
    resolutions, and a PAGED SUBTREE presence enumeration (the sweep)."""

    def __init__(self, group_entries, member_entries, present_emails):
        self.group_entries = group_entries
        self.member_entries = member_entries
        self.present_emails = present_emails
        self.entries: list = []
        self.result: dict = {}
        self.bind_calls = 0
        self.unbind_calls = 0
        self.search_calls = 0
        self._paged_done = False

    def start_tls(self):
        return True

    def bind(self):
        self.bind_calls += 1
        return True

    def search(
        self, *, search_base, search_filter, search_scope, attributes, paged_size=None, **_kw
    ):
        self.search_calls += 1
        from ldap3 import BASE

        if search_scope == BASE:
            store = self.group_entries if search_base in self.group_entries else self.member_entries
            entry = store.get(search_base)
            if entry is None:
                self.entries = []
                self.result = {"result": RESULT_NO_SUCH_OBJECT}
                return False
            self.entries = [_FakeEntry(search_base, entry)]
            self.result = {"result": RESULT_SUCCESS}
            return True
        # SUBTREE paged presence enumeration: one page, no cookie.
        if self._paged_done:
            self.entries = []
            self.result = {"result": RESULT_SUCCESS, "controls": {}}
            return False
        self._paged_done = True
        self.entries = [_FakeEntry("presence", {"mail": [e]}) for e in self.present_emails]
        self.result = {"result": RESULT_SUCCESS, "controls": {}}
        return bool(self.entries)

    def unbind(self):
        self.unbind_calls += 1
        return True


@pytest.mark.asyncio
async def test_run_with_mapping_and_sweep_shares_one_connection(monkeypatch):
    from app.database import Base
    from app.models.group import UserGroup
    from app.models.ldap_group_mapping import LdapGroupMapping
    from app.models.user import User
    from app.services import ldap_sync_service
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_deactivation_max_percent", 100, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_deactivation_min_count", 1000, raising=False)
    monkeypatch.setattr(settings, "ldap_disabled_filter", "", raising=False)
    monkeypatch.setattr(
        settings, "ldap_user_base_dn", "ou=people,dc=company,dc=local", raising=False
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        group = UserGroup(name="Engineering")
        db.add(group)
        await db.commit()
        mapping = LdapGroupMapping(
            group_dn=_group_dn(1), directory_name="herd-eng", herd_group_id=group.id
        )
        db.add(mapping)
        existing = User(email="user1@company.local", username="user1", auth_source="ldap")
        db.add(existing)
        await db.commit()

        group_entries = {_group_dn(1): _group_entry([_member_dn(1)])}
        member_entries = {_member_dn(1): _member_entry(1)}
        conn_fake = _FakeGroupSyncConnection(group_entries, member_entries, ["user1@company.local"])
        constructed = _patch_connection_factory(monkeypatch, [conn_fake])

        # run_sync opens the run with the stale-run reap (issue #528), which
        # deliberately runs on its OWN session from app.database. That points
        # at a different in-memory SQLite database than the engine built
        # above, so the reap would find no ldap_sync_runs table; it is
        # best-effort and swallows that, but stubbing it keeps a red-herring
        # traceback out of this test's captured log. What this test pins is
        # the directory CONNECTION count, which the reap never touches.
        async def _no_reap() -> int:
            return 0

        monkeypatch.setattr(ldap_sync_service, "_reap_stale_running_runs_on_own_session", _no_reap)

        run = await ldap_sync_service.run_sync(db)

    assert run.status in ("success", "partial")
    # One Connection() construction, one bind, for the WHOLE run: the
    # mapping loop's fetch_group + resolve_members AND the sweep's
    # present_emails all shared it (issue #513 item 7).
    assert len(constructed) == 1
    assert conn_fake.bind_calls == 1
    assert conn_fake.unbind_calls == 1

    await engine.dispose()


# ---------------------------------------------------------------------------
# Item 8: a reconnect attempt whose OWN bind also fails closes BOTH
# connections (the original live one, torn down by reconnect()'s
# close-then-get; the fresh one, torn down by _open_service_connection's
# own bind-failure cleanup).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_bind_failure_after_transport_drop_closes_both_connections(monkeypatch):
    entries = {_group_dn(1): _group_entry([_member_dn(1)]), _member_dn(1): _member_entry(1)}
    # first becomes live via search #1, then drops transport-wise on
    # search #2, triggering a reconnect; the reconnect's bind itself fails.
    first = _FakeConnection(entries, raise_on_search_calls={2: LDAPSocketReceiveError})
    second = _FakeConnection(entries, bind_ok=False)
    constructed = _patch_connection_factory(monkeypatch, [first, second])

    async with ldap_service.run_connection() as holder:
        entry = await ldap_service.fetch_group(_group_dn(1), run_holder=holder)
        assert entry is not None
        with pytest.raises(ldap_service.LdapUnavailableError):
            await ldap_service.resolve_members(entry.member_dns, run_holder=holder)

    assert len(constructed) == 2
    assert first.bind_calls == 1
    assert first.unbind_calls == 1
    assert second.bind_calls == 1
    assert second.unbind_calls == 1
