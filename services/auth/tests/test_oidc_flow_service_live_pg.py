"""Postgres-live proof that the OIDC flow store and JIT provisioning hold under
real concurrency (ADR 0016 phase 2, issue #37).

The SQLite suites (test_oidc_flow_service.py, test_oidc_provisioning.py) pin
every status by operation cell and every collision deterministically, but
SQLite never makes two sessions contend for one row. Here the races run as REAL
concurrent asyncio tasks over SEPARATE Postgres sessions, through the real
production functions:

- N claims of one state: exactly one gets the flow; the row ends claimed with
  no verifier. Wrong-binding claims racing the right one change nothing.
- N exchanges of one handoff: exactly one gets the user.
- complete versus fail on one claimed row: exactly one wins.
- N first logins of one new (issuer, subject): one user, one identity, and
  every caller gets that same user; N first logins of different subjects with
  one email: one account, every other caller refused.

Contention is forced, not hoped for: a holder transaction locks the flow row
(`SELECT ... FOR UPDATE`) or the users table (`LOCK TABLE users IN SHARE ROW
EXCLUSIVE MODE`, which blocks inserts and not reads) while the N tasks start,
the test waits until all N are blocked on that lock in `pg_stat_activity`, and
only then releases it. Every task has therefore done its read and its checks
before any of them writes, which is the window a compare-and-swap must close.

Env contract identical to services/reservations/tests/test_reservation_status_cas_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Gate-ledger scoping (TESTING.md, issue #819). The gate runs this suite against
its ALREADY-USED auth schema. Every test owns rows under a random tag (users
under `@<tag>.example.invalid`, identities under an issuer naming the tag, flows
by id), and the `ledger` fixture also seeds FOREIGN rows before the test: a flow
in every status, a local, an ldap, and an oidc account with its identity. Every
foreign row, those and whatever the gate already holds, is snapshotted before
and compared after each test; a foreign flow whose expires_at is more than 50
minutes past is left out of the snapshot, since `start_flow` may legitimately
prune it. Every row the test or the fixture created is deleted in a finally
(deleting the users cascades to their identities, flows, refresh tokens, and
group memberships). The users-table lock is held for well under a second.

The engine pins search_path to the auth schema: the models are mapped
schema-less by the SQLite-oriented conftest. lock_timeout and statement_timeout
bound every wait, so a lost race fails in seconds instead of hanging the gate.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.federated_identity import FederatedIdentity
from app.models.oidc_login_flow import (
    FLOW_AUTHENTICATED,
    FLOW_CLAIMED,
    FLOW_EXCHANGED,
    FLOW_FAILED,
    FLOW_PENDING,
    FLOW_STATUSES,
    OidcLoginFlow,
)
from app.models.user import Role, User
from app.services.auth_service import (
    FederatedLogin,
    create_ldap_user,
    create_oidc_user,
    create_user,
    resolve_federated_user,
)
from app.services.oidc_flow_service import (
    PRUNE_AFTER,
    claim_flow,
    complete_flow,
    exchange_handoff,
    fail_flow,
    start_flow,
)
from app.utils.jwt import hash_token
from sqlalchemy import func, select, text, true
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

N = 6
VERIFIER = "v" * 64


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
        missing = [
            name
            for name in ("oidc_login_flows", "federated_identities")
            if (await conn.execute(text("SELECT to_regclass(:n)"), {"n": f"auth.{name}"})).scalar()
            is None
        ]
    if missing:
        await engine.dispose()
        pytest.fail(
            f"auth schema lacks {missing}: the stack's auth image predates migration 0010 "
            "(rebuild auth, or run make migrate-auth)."
        )
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    """Own rows under a random tag, the seeded foreign rows, and the snapshot."""

    def __init__(self, session_factory) -> None:
        self.sf = session_factory
        self.tag = uuid.uuid4().hex[:12]
        self.foreign_tag = uuid.uuid4().hex[:12]
        self.issuer = f"https://oidc-live-{self.tag}.example.invalid"
        self.flow_ids: set[uuid.UUID] = set()
        self.foreign_flow_ids: set[uuid.UUID] = set()
        self.before: dict | None = None

    def email(self, local: str) -> str:
        return f"{local}@{self.tag}.example.invalid"

    def login(self, subject: str, *, email: str | None = None, username: str | None = None):
        return FederatedLogin(
            issuer=self.issuer,
            subject=subject,
            email=email or self.email(subject),
            email_verified=True,
            username=username or f"{subject}-{self.tag}",
            full_name=None,
        )

    async def user(self, local: str, *, active: bool = True) -> uuid.UUID:
        async with self.sf() as db:
            u = User(
                email=self.email(local),
                username=f"{local}-{self.tag}",
                auth_source="oidc",
                role=Role.USER,
                is_active=active,
            )
            db.add(u)
            await db.commit()
            return u.id

    async def start(self, state: str, binding: str, next_path: str = "/reservations"):
        async with self.sf() as db:
            flow_id = await start_flow(
                db,
                state=state,
                nonce=f"nonce-{state}",
                code_verifier=VERIFIER,
                binding_secret=binding,
                next_path=next_path,
            )
        self.flow_ids.add(flow_id)
        return flow_id

    async def insert_flow(
        self, status: str, *, user_id, expires_at: datetime, own: bool = True
    ) -> tuple[uuid.UUID, str, str, str]:
        state, binding, handoff = (uuid.uuid4().hex for _ in range(3))
        async with self.sf() as db:
            row = OidcLoginFlow(
                state_hash=hash_token(state),
                binding_hash=hash_token(binding),
                nonce="n",
                code_verifier=VERIFIER,
                next_path="/reservations",
                status=status,
                user_id=user_id,
                handoff_hash=hash_token(handoff),
                expires_at=expires_at,
                handoff_expires_at=expires_at,
            )
            db.add(row)
            await db.commit()
        (self.flow_ids if own else self.foreign_flow_ids).add(row.id)
        return row.id, state, binding, handoff

    async def seed_foreign(self) -> None:
        """Rows the tests must never touch: a live flow in every status for a
        foreign user, and one account of each source (the oidc one with its
        identity), all under the foreign tag."""
        domain = f"{self.foreign_tag}.example.invalid"
        async with self.sf() as db:
            await create_user(db, f"local@{domain}", f"local-{self.foreign_tag}", "password123")
        async with self.sf() as db:
            await create_ldap_user(db, f"ldap@{domain}", f"ldap-{self.foreign_tag}")
        async with self.sf() as db:
            oidc = await create_oidc_user(
                db,
                FederatedLogin(
                    issuer=f"https://oidc-live-{self.foreign_tag}.example.invalid",
                    subject="foreign-subject",
                    email=f"oidc@{domain}",
                    email_verified=True,
                    username=f"oidc-{self.foreign_tag}",
                    full_name=None,
                ),
            )
        for status in FLOW_STATUSES:
            await self.insert_flow(
                status, user_id=oidc.id, expires_at=_now() + timedelta(minutes=10), own=False
            )

    async def snapshot(self) -> dict:
        cutoff = _now() - timedelta(minutes=50)
        own_flows = list(self.flow_ids)
        own_domain = f"%@{self.tag}.example.invalid"
        async with self.sf() as db:
            flows = await db.execute(
                select(
                    OidcLoginFlow.id,
                    OidcLoginFlow.status,
                    OidcLoginFlow.state_hash,
                    OidcLoginFlow.binding_hash,
                    OidcLoginFlow.handoff_hash,
                    OidcLoginFlow.code_verifier,
                    OidcLoginFlow.user_id,
                    OidcLoginFlow.expires_at,
                    OidcLoginFlow.handoff_expires_at,
                ).where(
                    OidcLoginFlow.id.not_in(own_flows) if own_flows else true(),
                    OidcLoginFlow.expires_at >= cutoff,
                )
            )
            users = await db.execute(
                select(
                    User.id,
                    User.email,
                    User.username,
                    User.full_name,
                    User.auth_source,
                    User.role,
                    User.is_active,
                    User.hashed_password,
                ).where(User.email.not_like(own_domain))
            )
            idents = await db.execute(
                select(
                    FederatedIdentity.id,
                    FederatedIdentity.user_id,
                    FederatedIdentity.issuer,
                    FederatedIdentity.subject,
                    FederatedIdentity.last_login_at,
                ).where(FederatedIdentity.issuer != self.issuer)
            )
            return {
                "flows": {r.id: tuple(r) for r in flows},
                "users": {r.id: tuple(r) for r in users},
                "identities": {r.id: tuple(r) for r in idents},
            }

    async def assert_foreign_untouched(self) -> None:
        after = await self.snapshot()
        assert after["users"] == self.before["users"]
        assert after["identities"] == self.before["identities"]
        # A foreign flow may only be absent after if start_flow could prune it.
        assert after["flows"] == self.before["flows"]
        assert self.foreign_flow_ids <= set(after["flows"])

    async def cleanup(self) -> None:
        async with self.sf() as db:
            ids = list(self.flow_ids | self.foreign_flow_ids)
            if ids:
                await db.execute(
                    text("DELETE FROM oidc_login_flows WHERE id = ANY(:ids)"), {"ids": ids}
                )
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


async def _wait_for_blocked(engine, statement_prefix: str, n: int, timeout: float = 10.0) -> None:
    """Wait until n backends of this database wait on a lock while running a
    statement that starts with `statement_prefix`."""
    deadline = time.monotonic() + timeout
    seen = -1
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            seen = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        " AND query LIKE :p"
                    ),
                    {"p": statement_prefix + "%"},
                )
            ).scalar_one()
        if seen >= n:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"only {seen} of {n} tasks reached the lock on {statement_prefix!r}")


async def _race_on_flow_row(engine, flow_id, calls, statement_prefix: str, n_blocking: int):
    """Hold the flow row's lock, start every call, wait until n_blocking of them
    are blocked on it, release, and return every result."""
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT id FROM oidc_login_flows WHERE id = :id FOR UPDATE"), {"id": flow_id}
        )
        tasks = [asyncio.create_task(c()) for c in calls]
        try:
            await _wait_for_blocked(engine, statement_prefix, n_blocking)
        finally:
            await holder.rollback()
        return await asyncio.gather(*tasks)


async def _flow(session_factory, flow_id) -> OidcLoginFlow:
    async with session_factory() as db:
        return (
            await db.execute(select(OidcLoginFlow).where(OidcLoginFlow.id == flow_id))
        ).scalar_one()


# --- flow store races --------------------------------------------------------------


async def test_concurrent_claims_of_one_state_have_exactly_one_winner(
    pg_engine, session_factory, ledger
):
    flow_id = await ledger.start("st-" + ledger.tag, "bi-" + ledger.tag)

    def claimer():
        async def call():
            async with session_factory() as db:
                return await claim_flow(
                    db, state="st-" + ledger.tag, binding_secret="bi-" + ledger.tag
                )

        return call

    results = await _race_on_flow_row(
        pg_engine, flow_id, [claimer() for _ in range(N)], "UPDATE oidc_login_flows", N
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, results
    assert winners[0].id == flow_id and winners[0].code_verifier == VERIFIER
    row = await _flow(session_factory, flow_id)
    assert row.status == FLOW_CLAIMED and row.code_verifier is None
    await ledger.assert_foreign_untouched()


async def test_free_running_claims_have_one_winner_per_flow(session_factory, ledger):
    flows = {}
    for i in range(10):
        state, binding = f"st{i}-{ledger.tag}", f"bi{i}-{ledger.tag}"
        flows[await ledger.start(state, binding)] = (state, binding)

    async def claim(state, binding):
        async with session_factory() as db:
            return await claim_flow(db, state=state, binding_secret=binding)

    results = await asyncio.gather(*(claim(s, b) for (s, b) in flows.values() for _ in range(4)))
    won = [r.id for r in results if r is not None]
    assert sorted(won) == sorted(flows)
    for flow_id in flows:
        assert (await _flow(session_factory, flow_id)).status == FLOW_CLAIMED
    await ledger.assert_foreign_untouched()


async def test_wrong_binding_claims_racing_the_right_one_change_nothing(
    pg_engine, session_factory, ledger
):
    state, binding = "st-" + ledger.tag, "bi-" + ledger.tag
    flow_id = await ledger.start(state, binding)

    async def claim(secret):
        async with session_factory() as db:
            return await claim_flow(db, state=state, binding_secret=secret)

    wrong = [claim("wrong-binding") for _ in range(N)]
    assert await asyncio.gather(*wrong) == [None] * N
    assert (await _flow(session_factory, flow_id)).status == FLOW_PENDING

    calls = [lambda: claim("wrong-binding") for _ in range(3)] + [lambda: claim(binding)]
    results = await _race_on_flow_row(pg_engine, flow_id, calls, "UPDATE oidc_login_flows", 1)
    assert [r is not None for r in results] == [False, False, False, True]
    assert (await _flow(session_factory, flow_id)).status == FLOW_CLAIMED
    await ledger.assert_foreign_untouched()


async def test_concurrent_exchanges_of_one_handoff_have_exactly_one_winner(
    pg_engine, session_factory, ledger
):
    uid = await ledger.user("exchanger")
    state, binding = "st-" + ledger.tag, "bi-" + ledger.tag
    flow_id = await ledger.start(state, binding, next_path="/devices")
    async with session_factory() as db:
        assert await claim_flow(db, state=state, binding_secret=binding) is not None
        assert await complete_flow(db, flow_id, user_id=uid, handoff_secret="h-" + ledger.tag)

    def exchanger():
        async def call():
            async with session_factory() as db:
                return await exchange_handoff(db, handoff_secret="h-" + ledger.tag)

        return call

    results = await _race_on_flow_row(
        pg_engine, flow_id, [exchanger() for _ in range(N)], "UPDATE oidc_login_flows", N
    )
    assert [r for r in results if r is not None] == [(uid, "/devices")]
    assert (await _flow(session_factory, flow_id)).status == FLOW_EXCHANGED
    await ledger.assert_foreign_untouched()


async def test_complete_racing_fail_on_one_claimed_row_has_exactly_one_winner(
    pg_engine, session_factory, ledger
):
    uid = await ledger.user("completer")
    for round_ in range(4):
        flow_id, *_ = await ledger.insert_flow(
            FLOW_CLAIMED, user_id=uid, expires_at=_now() + timedelta(minutes=10)
        )

        async def complete():
            async with session_factory() as db:
                return await complete_flow(
                    db, flow_id, user_id=uid, handoff_secret=f"h{round_}-{ledger.tag}"
                )

        async def fail():
            async with session_factory() as db:
                return await fail_flow(db, flow_id)

        calls = [complete, fail, complete, fail] if round_ % 2 else [fail, complete, fail, complete]
        results = await _race_on_flow_row(pg_engine, flow_id, calls, "UPDATE oidc_login_flows", 4)
        assert results.count(True) == 1, results
        status = (await _flow(session_factory, flow_id)).status
        winner = calls[results.index(True)]
        assert status == (FLOW_AUTHENTICATED if winner is complete else FLOW_FAILED)
    await ledger.assert_foreign_untouched()


async def test_expired_rows_are_neither_claimable_nor_exchangeable_on_timestamptz(
    session_factory, ledger
):
    uid = await ledger.user("expired")
    past = _now() - timedelta(seconds=5)
    _, state, binding, _ = await ledger.insert_flow(FLOW_PENDING, user_id=uid, expires_at=past)
    auth_id, _, _, handoff = await ledger.insert_flow(
        FLOW_AUTHENTICATED, user_id=uid, expires_at=past
    )
    async with session_factory() as db:
        assert await claim_flow(db, state=state, binding_secret=binding) is None
        assert await exchange_handoff(db, handoff_secret=handoff) is None
    assert (await _flow(session_factory, auth_id)).status == FLOW_AUTHENTICATED
    await ledger.assert_foreign_untouched()


async def test_start_flow_prunes_only_rows_more_than_an_hour_expired(session_factory, ledger):
    """The gate holds no other prunable flow (the feature is dark), so one
    start_flow's batch of 500 reaches every prunable row seeded here."""
    uid = await ledger.user("pruner")
    now = _now()
    prunable, kept = set(), set()
    for status in FLOW_STATUSES:
        old, *_ = await ledger.insert_flow(
            status, user_id=uid, expires_at=now - PRUNE_AFTER - timedelta(minutes=2)
        )
        recent, *_ = await ledger.insert_flow(
            status, user_id=uid, expires_at=now - PRUNE_AFTER + timedelta(minutes=2)
        )
        live, *_ = await ledger.insert_flow(
            status, user_id=uid, expires_at=now + timedelta(minutes=5)
        )
        prunable.add(old)
        kept |= {recent, live}
    await ledger.start("st-" + ledger.tag, "bi-" + ledger.tag)
    async with session_factory() as db:
        present = set(
            (
                await db.execute(
                    select(OidcLoginFlow.id).where(OidcLoginFlow.id.in_(prunable | kept))
                )
            )
            .scalars()
            .all()
        )
    assert present == kept
    await ledger.assert_foreign_untouched()


# --- provisioning races ----------------------------------------------------------------


async def _race_on_users_insert(pg_engine, calls, n_blocking: int):
    """Block inserts into users (reads still pass) while every call runs its
    lookups and collision checks, wait until n_blocking calls are blocked on
    the INSERT, then release."""
    async with pg_engine.connect() as holder:
        await holder.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
        tasks = [asyncio.create_task(c()) for c in calls]
        try:
            await _wait_for_blocked(pg_engine, "INSERT INTO users", n_blocking)
        finally:
            await holder.rollback()
        return await asyncio.gather(*tasks)


async def _count_where(session_factory, *conditions, model=User) -> int:
    async with session_factory() as db:
        return (
            await db.execute(select(func.count()).select_from(model).where(*conditions))
        ).scalar_one()


async def test_concurrent_first_logins_of_one_identity_create_one_user(
    pg_engine, session_factory, ledger
):
    login = ledger.login("first-login")

    def resolver():
        async def call():
            async with session_factory() as db:
                user = await resolve_federated_user(db, login)
                return None if user is None else user.id

        return call

    results = await _race_on_users_insert(pg_engine, [resolver() for _ in range(N)], N)
    assert None not in results, results
    assert len(set(results)) == 1
    assert await _count_where(session_factory, func.lower(User.email) == login.email.lower()) == 1
    assert (
        await _count_where(
            session_factory,
            FederatedIdentity.issuer == login.issuer,
            FederatedIdentity.subject == login.subject,
            model=FederatedIdentity,
        )
        == 1
    )
    async with session_factory() as db:
        user = (await db.execute(select(User).where(User.id == results[0]))).scalar_one()
    assert (user.auth_source, user.hashed_password, user.role) == ("oidc", None, Role.USER)
    await ledger.assert_foreign_untouched()


async def test_concurrent_first_logins_of_different_subjects_with_one_email_create_one_user(
    pg_engine, session_factory, ledger
):
    email = ledger.email("shared")
    logins = [ledger.login(f"subject-{i}", email=email) for i in range(N)]

    def resolver(login):
        async def call():
            async with session_factory() as db:
                user = await resolve_federated_user(db, login)
                return None if user is None else user.id

        return call

    results = await _race_on_users_insert(pg_engine, [resolver(lg) for lg in logins], N)
    assert len([r for r in results if r is not None]) == 1, results
    assert await _count_where(session_factory, User.email == email) == 1
    assert (
        await _count_where(
            session_factory, FederatedIdentity.issuer == ledger.issuer, model=FederatedIdentity
        )
        == 1
    )
    await ledger.assert_foreign_untouched()


@pytest.mark.parametrize("source", ["local", "ldap", "oidc"])
async def test_case_insensitive_email_collision_on_postgres(session_factory, ledger, source):
    existing_email = ledger.email("Mixed.Case")
    async with session_factory() as db:
        if source == "local":
            await create_user(db, existing_email, f"existing-{ledger.tag}", "password123")
        elif source == "ldap":
            await create_ldap_user(db, existing_email, f"existing-{ledger.tag}")
        else:
            await create_oidc_user(db, ledger.login("existing", email=existing_email))
    async with session_factory() as db:
        result = await resolve_federated_user(
            db, ledger.login("newcomer", email=existing_email.lower())
        )
    assert result is None
    assert (
        await _count_where(
            session_factory,
            FederatedIdentity.issuer == ledger.issuer,
            FederatedIdentity.subject == "newcomer",
            model=FederatedIdentity,
        )
        == 0
    )
    await ledger.assert_foreign_untouched()
