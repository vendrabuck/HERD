"""Postgres-live proof that the federated-identity recovery holds under real
concurrency (issue #1399).

The SQLite suites (test_federated_identity_service.py, test_federated_identity_routes.py)
pin every refusal and outcome, but SQLite never makes two sessions contend.
Here the races run as REAL concurrent asyncio tasks over SEPARATE Postgres
sessions, through the production functions, and every test ends by checking
the invariant the routes promise: each account has at most one identity, no two
accounts share an (issuer, subject), and no account id changed.

- N relinks of N accounts to one (issuer, subject): exactly one wins; every
  loser keeps the identity it had (its delete rolled back with its insert).
- N relinks of ONE account to N pairs: they apply in turn on the account's row
  lock, each succeeds, and the account ends with exactly one of the N pairs.
- A rewrite whose UPDATE runs after a first login committed a colliding
  (new issuer, subject): deterministic, through a wrapper around the real
  count; the UPDATE fails on the constraint, nothing is written, and the
  conflict is reported.
- A rewrite and that first login released together from a table lock: either
  order is allowed, the invariant must hold in both.
- A rewrite on a used ledger moves only its own exact issuer.

Contention is forced, not hoped for: a holder transaction takes
`LOCK TABLE federated_identities IN SHARE ROW EXCLUSIVE MODE` (blocks every
write, no read) or the account row `FOR UPDATE` while the tasks start, the test
waits until they are all blocked in `pg_stat_activity`, and only then releases.

Env contract identical to test_oidc_flow_service_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (TESTING.md, issue #819): the gate runs this against its
ALREADY-USED auth schema. Every test owns its accounts under
`@<tag>.example.invalid` and its identities under issuers naming the tag; the
`ledger` fixture also seeds FOREIGN rows first: a local, an ldap, and two oidc
accounts, one of whose identities has the SAME subject as the test's own rows
under a foreign issuer, and the other the same issuer string with a trailing
slash removed or added. Every foreign users and federated_identities row, those
and whatever the gate already holds, is snapshotted before and compared after
each test. Every row the test or the fixture created is deleted in a finally.
The table lock is held for well under a second per race.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from app.models.federated_identity import FederatedIdentity
from app.models.user import Role, User
from app.services import federated_identity_service
from app.services.auth_service import (
    FederatedLogin,
    create_ldap_user,
    create_user,
    resolve_federated_user,
)
from app.services.federated_identity_service import (
    IssuerRewriteConflict,
    RelinkRefused,
    relink_identity,
    rewrite_issuer,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")

_CONNECT_ARGS = {
    "server_settings": {
        "search_path": "auth",
        "lock_timeout": "10s",
        "statement_timeout": "30s",
    }
}

N = 5


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS, pool_size=N + 4)


async def _pg_reachable() -> bool:
    engine = _make_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


_PG_REACHABLE = asyncio.run(_pg_reachable())

pytestmark = pytest.mark.skipif(
    not _PG_REQUIRED and not _PG_REACHABLE,
    reason=(
        f"No Postgres reachable at {PG_DSN!r}; set HERD_TEST_PG_DSN to point at one "
        "(e.g. the gate stack's published postgres port, 5433) to run this suite."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _PG_REQUIRED and not _PG_REACHABLE:
        pytest.fail(
            f"HERD_TEST_PG_REQUIRED is set but no Postgres is reachable at {PG_DSN!r}; "
            "start one or unset HERD_TEST_PG_REQUIRED."
        )


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    async with engine.connect() as conn:
        present = (
            await conn.execute(text("SELECT to_regclass('auth.federated_identities')"))
        ).scalar()
    if present is None:
        await engine.dispose()
        pytest.fail(
            "auth schema lacks federated_identities: the stack's auth image predates "
            "migration 0010 (rebuild auth, or run make migrate-auth)."
        )
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


class Ledger:
    """Own rows under a random tag, the seeded foreign rows, and the snapshot."""

    def __init__(self, session_factory) -> None:
        self.sf = session_factory
        self.tag = uuid.uuid4().hex[:16]
        self.foreign_tag = uuid.uuid4().hex[:16]
        self.old = f"https://fi-live-{self.tag}.example.invalid/realms/herd"
        self.new = self.old + "/"
        self.before: dict | None = None

    def email(self, local: str) -> str:
        return f"{local}@{self.tag}.example.invalid"

    def _own_user_ids(self):
        """The ids of this test's accounts, the strays its first logins create
        included (every one has an email under the tag), as a subquery."""
        return select(User.id).where(User.email.like(f"%@{self.tag}.example.invalid"))

    async def account(self, local: str, issuer: str | None = None, subject: str | None = None):
        """An oidc account of this test, with an identity unless issuer is None."""
        async with self.sf() as db:
            user = User(
                email=self.email(local),
                username=f"{local}-{self.tag}",
                full_name=local,
                auth_source="oidc",
                role=Role.USER,
            )
            db.add(user)
            await db.flush()
            if issuer is not None:
                db.add(
                    FederatedIdentity(
                        user_id=user.id, issuer=issuer, subject=subject or f"sub-{local}"
                    )
                )
            await db.commit()
            return user.id

    def login(self, local: str, *, issuer: str, subject: str) -> FederatedLogin:
        return FederatedLogin(
            issuer=issuer,
            subject=subject,
            email=self.email(local),
            email_verified=True,
            username=f"{local}-{self.tag}",
            full_name=None,
        )

    async def seed_foreign(self) -> None:
        domain = f"{self.foreign_tag}.example.invalid"
        async with self.sf() as db:
            await create_user(db, f"local@{domain}", f"local-{self.foreign_tag}", "password123")
        async with self.sf() as db:
            await create_ldap_user(db, f"ldap@{domain}", f"ldap-{self.foreign_tag}")
        # The subject of the tests' own first row, under an issuer with another
        # host and under one that extends the tests' old issuer by a path
        # segment: an exact-match rewrite must leave both alone.
        for name, issuer in (
            ("same-subject", f"https://fi-live-{self.foreign_tag}.example.invalid/realms/herd"),
            ("near-issuer", self.old.rstrip("/") + "/x"),
        ):
            async with self.sf() as db:
                user = User(
                    email=f"{name}@{domain}",
                    username=f"{name}-{self.foreign_tag}",
                    full_name=name,
                    auth_source="oidc",
                    role=Role.USER,
                )
                db.add(user)
                await db.flush()
                db.add(FederatedIdentity(user_id=user.id, issuer=issuer, subject="sub-a"))
                await db.commit()

    async def snapshot(self) -> dict:
        own_domain = f"%@{self.tag}.example.invalid"
        async with self.sf() as db:
            users = await db.execute(
                select(
                    User.id,
                    User.email,
                    User.username,
                    User.auth_source,
                    User.role,
                    User.is_active,
                ).where(User.email.not_like(own_domain))
            )
            idents = await db.execute(
                select(
                    FederatedIdentity.id,
                    FederatedIdentity.user_id,
                    FederatedIdentity.issuer,
                    FederatedIdentity.subject,
                    FederatedIdentity.created_at,
                    FederatedIdentity.last_login_at,
                ).where(FederatedIdentity.user_id.not_in(self._own_user_ids()))
            )
            return {
                "users": {r.id: tuple(r) for r in users},
                "identities": {r.id: tuple(r) for r in idents},
            }

    async def assert_foreign_untouched(self) -> None:
        after = await self.snapshot()
        assert after == self.before

    async def own_identities(self) -> list[tuple[uuid.UUID, str, str]]:
        async with self.sf() as db:
            rows = await db.execute(
                select(
                    FederatedIdentity.user_id, FederatedIdentity.issuer, FederatedIdentity.subject
                ).where(FederatedIdentity.user_id.in_(self._own_user_ids()))
            )
            return [tuple(r) for r in rows]

    async def own_account_ids(self) -> set[uuid.UUID]:
        async with self.sf() as db:
            return set(
                (
                    await db.execute(
                        select(User.id).where(User.email.like(f"%@{self.tag}.example.invalid"))
                    )
                )
                .scalars()
                .all()
            )

    async def assert_invariant(self) -> None:
        """At most one identity per account; no (issuer, subject) twice."""
        rows = await self.own_identities()
        users = [r[0] for r in rows]
        pairs = [(r[1], r[2]) for r in rows]
        assert len(users) == len(set(users)), rows
        assert len(pairs) == len(set(pairs)), rows
        async with self.sf() as db:
            dupes = (
                await db.execute(
                    select(func.count())
                    .select_from(FederatedIdentity)
                    .group_by(FederatedIdentity.issuer, FederatedIdentity.subject)
                    .having(func.count() > 1)
                )
            ).all()
        assert dupes == []

    async def cleanup(self) -> None:
        async with self.sf() as db:
            for tag in (self.tag, self.foreign_tag):
                await db.execute(
                    text("DELETE FROM users WHERE email LIKE :d"),
                    {"d": f"%@{tag}.example.invalid"},
                )
            await db.commit()


@pytest.fixture
async def ledger(session_factory):
    lg = Ledger(session_factory)
    try:
        await lg.seed_foreign()
        lg.before = await lg.snapshot()
        yield lg
    finally:
        await lg.cleanup()


async def _wait_for_blocked(engine, prefixes: tuple[str, ...], n: int, timeout: float = 10.0):
    """Wait until n backends of this database wait on a lock while running a
    statement that starts with one of `prefixes`."""
    deadline = time.monotonic() + timeout
    seen = -1
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            seen = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        " AND query LIKE ANY(:p)"
                    ),
                    {"p": [p + "%" for p in prefixes]},
                )
            ).scalar_one()
        if seen >= n:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"only {seen} of {n} tasks reached the lock on {prefixes!r}")


async def _race_on_table(engine, calls, prefixes: tuple[str, ...], n_blocking: int):
    """Block every write to federated_identities, start every call, wait until
    n_blocking are blocked, release, and return every outcome (result or error)."""
    async with engine.connect() as holder:
        await holder.execute(text("LOCK TABLE federated_identities IN SHARE ROW EXCLUSIVE MODE"))
        tasks = [asyncio.create_task(c()) for c in calls]
        try:
            await _wait_for_blocked(engine, prefixes, n_blocking)
        finally:
            await holder.rollback()
        return await asyncio.gather(*tasks, return_exceptions=True)


# --- the tests ---------------------------------------------------------------------


async def test_rewrite_on_a_used_ledger_moves_only_its_own_exact_issuer(session_factory, ledger):
    ids = [await ledger.account(name, ledger.old) for name in ("a", "b", "c")]
    async with session_factory() as db:
        result = await rewrite_issuer(
            db, old_issuer=ledger.old, new_issuer=ledger.new, dry_run=False
        )
    assert (result.matched, result.conflicts, result.rewritten) == (3, 0, 3)
    rows = await ledger.own_identities()
    assert sorted(rows) == sorted((i, ledger.new, f"sub-{n}") for i, n in zip(ids, "abc"))
    assert await ledger.own_account_ids() == set(ids)
    await ledger.assert_invariant()
    await ledger.assert_foreign_untouched()


async def test_rewrite_after_a_first_login_committed_a_colliding_pair_writes_nothing(
    monkeypatch, session_factory, ledger
):
    """Deterministic: the real conflict count runs, then a first login of
    (new issuer, sub-a) with another email commits a stray account, then the
    UPDATE runs and fails on the unique constraint."""
    a = await ledger.account("a", ledger.old)
    b = await ledger.account("b", ledger.old)
    real_count = federated_identity_service._count_conflicts
    strays = []

    async def count_then_a_first_login_commits(db, old_issuer, new_issuer):
        counted = await real_count(db, old_issuer, new_issuer)
        if not strays:
            async with session_factory() as other:
                stray = await resolve_federated_user(
                    other, ledger.login("stray", issuer=ledger.new, subject="sub-a")
                )
            strays.append(stray.id)
        return counted

    monkeypatch.setattr(
        federated_identity_service, "_count_conflicts", count_then_a_first_login_commits
    )
    async with session_factory() as db:
        with pytest.raises(IssuerRewriteConflict) as raised:
            await rewrite_issuer(db, old_issuer=ledger.old, new_issuer=ledger.new, dry_run=False)
        assert not db.in_transaction()
    assert raised.value.conflicts == 1
    rows = sorted(await ledger.own_identities())
    assert rows == sorted(
        [(a, ledger.old, "sub-a"), (b, ledger.old, "sub-b"), (strays[0], ledger.new, "sub-a")]
    )
    await ledger.assert_invariant()
    await ledger.assert_foreign_untouched()


async def test_a_rewrite_and_a_colliding_first_login_released_together_keep_the_invariant(
    pg_engine, session_factory, ledger
):
    a = await ledger.account("a", ledger.old)

    async def rewrite():
        async with session_factory() as db:
            return await rewrite_issuer(
                db, old_issuer=ledger.old, new_issuer=ledger.new, dry_run=False
            )

    async def first_login():
        async with session_factory() as db:
            return await resolve_federated_user(
                db, ledger.login("stray", issuer=ledger.new, subject="sub-a")
            )

    outcomes = await _race_on_table(
        pg_engine,
        [rewrite, first_login],
        ("UPDATE federated_identities", "INSERT INTO federated_identities"),
        2,
    )
    rewrite_outcome, login_outcome = outcomes
    rows = sorted(await ledger.own_identities())
    if isinstance(rewrite_outcome, IssuerRewriteConflict):
        # The first login inserted first: a stray account holds the new pair,
        # and the rewrite changed nothing.
        assert login_outcome.id != a
        assert rows == sorted([(a, ledger.old, "sub-a"), (login_outcome.id, ledger.new, "sub-a")])
    else:
        # The rewrite updated first: the login lost the insert, found the
        # rewritten identity, and signed in as the same account.
        assert rewrite_outcome.rewritten == 1
        assert login_outcome is not None and login_outcome.id == a
        assert rows == [(a, ledger.new, "sub-a")]
    assert a in await ledger.own_account_ids()
    await ledger.assert_invariant()
    await ledger.assert_foreign_untouched()


async def test_concurrent_relinks_of_many_accounts_to_one_pair_have_exactly_one_winner(
    pg_engine, session_factory, ledger
):
    ids = [await ledger.account(f"u{i}", ledger.old) for i in range(N)]

    def relink(user_id):
        async def call():
            async with session_factory() as db:
                return await relink_identity(db, user_id, issuer=ledger.old, subject="contested")

        return call

    outcomes = await _race_on_table(
        pg_engine, [relink(i) for i in ids], ("DELETE FROM federated_identities",), N
    )
    winners = [i for i, o in zip(ids, outcomes) if not isinstance(o, BaseException)]
    losers = [(i, o) for i, o in zip(ids, outcomes) if isinstance(o, BaseException)]
    assert len(winners) == 1, outcomes
    assert all(isinstance(o, RelinkRefused) and o.reason == "identity_taken" for _, o in losers)
    rows = dict((r[0], (r[1], r[2])) for r in await ledger.own_identities())
    assert rows[winners[0]] == (ledger.old, "contested")
    for i, n in zip(ids, range(N)):
        if i != winners[0]:
            assert rows[i] == (ledger.old, f"sub-u{n}")
    await ledger.assert_invariant()
    await ledger.assert_foreign_untouched()


async def test_concurrent_relinks_of_one_account_apply_in_turn(pg_engine, session_factory, ledger):
    a = await ledger.account("a", ledger.old)
    pairs = [(ledger.new, f"pair-{i}") for i in range(N)]

    def relink(issuer, subject):
        async def call():
            async with session_factory() as db:
                return await relink_identity(db, a, issuer=issuer, subject=subject)

        return call

    # The table lock stops every task before its first write. With the account
    # row lock, one task holds the account and waits at its DELETE while the
    # others wait for the account; without it, all N would wait at the DELETE
    # and, released together, all but one would fail on the user_id
    # constraint. Either way N are blocked before the release.
    outcomes = await _race_on_table(
        pg_engine,
        [relink(*p) for p in pairs],
        ("SELECT users.id, users.auth_source", "DELETE FROM federated_identities"),
        N,
    )
    assert all(not isinstance(o, BaseException) and o.changed for o in outcomes), outcomes
    rows = await ledger.own_identities()
    assert len(rows) == 1
    assert rows[0][0] == a and (rows[0][1], rows[0][2]) in pairs
    await ledger.assert_invariant()
    await ledger.assert_foreign_untouched()


async def test_a_relink_refused_as_taken_on_postgres_keeps_the_old_identity(
    session_factory, ledger
):
    a = await ledger.account("a", ledger.old)
    b = await ledger.account("b", ledger.old)
    async with session_factory() as db:
        with pytest.raises(RelinkRefused) as raised:
            await relink_identity(db, a, issuer=ledger.old, subject="sub-b")
        assert not db.in_transaction()
    assert raised.value.reason == "identity_taken"
    assert sorted(await ledger.own_identities()) == sorted(
        [(a, ledger.old, "sub-a"), (b, ledger.old, "sub-b")]
    )
    await ledger.assert_foreign_untouched()
