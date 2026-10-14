"""Postgres-live proof that the retry tick's selection predicate equals its drive rule.

Issue #1190. `run_wiring_retry_tick` selected "oldest FAILED rows under the attempts
cap, LIMIT batch" from each wiring ledger and then dropped, in Python, the rows it
would never drive: a pinned non-retryable reason, and a build-direction row of a
frozen reservation. The skip left such a row as it was, so one batch of them at the
old end of a ledger hid every newer retryable row on every later tick. The rule now
sits in the three due queries (wiring_due.tick_would_drive).

The SQLite suite (tests/test_wiring_due.py) pins the same invariant, but three of the
ways the SQL can differ from the Python are exactly where SQLite cannot speak for
Postgres:

  - the prefix test's case-sensitivity (LIKE is case-insensitive for ASCII on SQLite
    and case-sensitive on Postgres, so the two disagree about a LIKE predicate);
  - NULL under three-valued logic, on the real column type;
  - FOR UPDATE SKIP LOCKED, which SQLite does not render at all: whether Postgres
    accepts the statement with its correlated subquery, and which rows it locks.

So this file runs the SAME enumerated cases (tests/_wiring_due_cases.py) through the
real due queries on a real Postgres, proves the starvation case with the LIMIT
applied, and shows from a second session what the select locks.

Env contract identical to test_wiring_retry_claim_race_live_pg.py:
    HERD_TEST_PG_DSN        SQLAlchemy asyncpg DSN.
    HERD_TEST_PG_REQUIRED   "1" (or any value not in ("", "0")) turns an unreachable
                            server into a hard failure instead of the normal skip.

Used-ledger rules (issue #819). This suite runs in `_gate-pg-live-tests` against the
gate's already-migrated, already-used execution schema, whose ledgers hold other
reservations' FAILED rows and whose execution service is running its own retry tick.

  - It calls the three due QUERIES only, never `run_wiring_retry_tick`: an unscoped
    tick on a used ledger would drive other reservations' rows.
  - The parity, starvation, and count tests insert their rows inside a transaction
    that is ROLLED BACK and never committed, so no other session (the stack's own
    tick included) can see, select, or drive them, and there is nothing to clean up.
  - The lock test must commit (a second session has to see the rows). Its ledger row
    carries an attempts value far above any real cap, so no real tick selects it, and
    it is deleted in a finally.
  - Assertions are scoped, never the SQL: the real statement runs unmodified and the
    test narrows the RESULT to its own ids. The parity and starvation tests call the
    due query with a large LIMIT, so for the few seconds their transaction is open they
    hold FOR UPDATE locks on every foreign row it returns; a concurrent writer of such
    a row (the stack's own tick skips them) waits that long. Where foreign rows are judged, only the
    direction a concurrent lock cannot break is asserted (a row the query returned
    must be one the tick would drive; SKIP LOCKED may hide a foreign row, never add
    one).
  - Every test snapshots every ledger row and wiring-state row it does not own,
    before and after, and asserts nothing about them changed.

Nothing here depends on foreign rows existing: on an empty database the foreign
checks are over an empty set and every own-row assertion is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.services import nats_consumer, wiring_due, wiring_retry_service
from app.services.nats_consumer import NON_RETRYABLE_REASON_PREFIXES
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests._wiring_due_cases import (
    BUILD,
    LEDGER_IDS,
    LEDGERS,
    RELEASE,
    Ledger,
    assert_parity,
    assert_reason_expectations_match_python,
    build_population,
    python_would_drive,
)

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")


async def _pg_reachable() -> bool:
    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _run_sync_reachable() -> bool:
    return asyncio.run(_pg_reachable())


_PG_REACHABLE = _run_sync_reachable()

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


_LEDGER_MODELS = (L1ConnectionAssignment, L2PortAssignment, RouteAssignment)
_OWN_TABLES = (*_LEDGER_MODELS, ReservationWiringState)


# The mapped tables carry no schema in this process: services/execution/conftest.py
# forces DB_SCHEMA="" for the SQLite unit suite, and Settings is fixed at first import.
# The live database's migrations created them under "execution". This suite does NOT
# flip `__table__.schema` to bridge that, as the claim suite does: the ORM memoizes
# table-derived constructs the first time a statement is built, so a schema flipped
# for one file can outlive the fixture and reach a later SQLite test in the same
# pytest run, which then fails with "no such table: execution.<table>" (seen here
# with l2_membership_service.count_active_memberships_for_vlan). Instead every
# connection this suite opens has its search_path set to the one schema, so the
# unqualified statements the due queries build resolve to the real tables and no
# shared mapper state is touched. It also means the suite does not depend on the
# database's own default search_path.
EXECUTION_SCHEMA = "execution"
# Every connection waits at most this long for a row lock. The due selects never wait
# (SKIP LOCKED), and nothing else here touches a row another session holds, so a wait
# means a regression: with SKIP LOCKED lost, the lock test fails in seconds instead of
# hanging the gate. Set per connection, so a COMMIT inside a test does not clear it.
LOCK_TIMEOUT = "5s"


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(
        PG_DSN,
        connect_args={
            "server_settings": {"search_path": EXECUTION_SCHEMA, "lock_timeout": LOCK_TIMEOUT}
        },
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database, the
    # production session factory shape every service actually runs with.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


CAP = 10
# An attempts value no real cap reaches. The one test that commits a FAILED ledger row
# gives it this many attempts, so the stack's own retry tick (which selects
# `attempts < WIRING_RETRY_MAX_ATTEMPTS`) can never select or drive it, and passes its
# own cap above it.
HIDDEN_ATTEMPTS = 1_000_000
# Older than any row a real stack holds, so an own row with this created_at is at the
# old end of its ledger whatever else the ledger contains.
ANCIENT = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Every reservation id a test generates, so the snapshot fixture knows what is "own".
_OWN_RESERVATIONS: set[uuid.UUID] = set()


def _own(*reservation_ids: uuid.UUID) -> None:
    _OWN_RESERVATIONS.update(reservation_ids)


async def _foreign_snapshot(session_factory) -> dict:
    """Every ledger row and wiring-state row this suite does not own, keyed by identity.

    On an empty Postgres this is an empty dict. On a used ledger it is the check that
    the suite changed nothing it did not create.
    """
    snapshot: dict = {}
    async with session_factory() as db:
        for model in _LEDGER_MODELS:
            result = await db.execute(select(model))
            for row in result.scalars().all():
                if row.reservation_id in _OWN_RESERVATIONS:
                    continue
                snapshot[(model.__tablename__, row.id)] = (
                    row.status,
                    row.intended,
                    row.attempts,
                    row.last_error,
                    row.claimed_until,
                )
        result = await db.execute(select(ReservationWiringState))
        for state in result.scalars().all():
            if state.reservation_id in _OWN_RESERVATIONS:
                continue
            snapshot[("reservation_wiring_state", state.reservation_id)] = (
                state.frozen,
                state.last_applied_fork_version,
            )
    return snapshot


async def _own_row_count(session_factory) -> int:
    total = 0
    async with session_factory() as db:
        for model in _OWN_TABLES:
            total += (
                await db.execute(
                    select(func.count(model.reservation_id)).where(
                        model.reservation_id.in_(_OWN_RESERVATIONS)
                    )
                )
            ).scalar_one()
    return total


@pytest.fixture(autouse=True)
async def _foreign_rows_untouched_and_nothing_left(session_factory):
    before = await _foreign_snapshot(session_factory)
    yield
    after = await _foreign_snapshot(session_factory)
    assert after == before, "this suite must never change a row it did not create"
    assert await _own_row_count(session_factory) == 0, "a test left its own rows behind"


async def _frozen_reservations(db) -> set[uuid.UUID]:
    result = await db.execute(
        select(ReservationWiringState.reservation_id).where(ReservationWiringState.frozen.is_(True))
    )
    return set(result.scalars().all())


def _add_population(db, ledger: Ledger, now: datetime, prefixes: tuple[str, ...] | None = None):
    if prefixes is None:
        prefixes = tuple(NON_RETRYABLE_REASON_PREFIXES)
    population = build_population(ledger, prefixes, now, CAP)
    _own(*population.reservation_ids.values())
    db.add_all(population.state_rows)
    db.add_all(population.rows)
    return population


# --- Parity -------------------------------------------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_due_query_returns_exactly_the_rows_the_tick_would_drive(ledger, session_factory):
    """The full case product on real Postgres: an own row is returned by the real due
    query if and only if it is FAILED, under the cap, unclaimed, and driven by the
    Python rule. Case variants of a pinned prefix, NULL and empty reasons, and rows of
    a reservation with no wiring-state row are all in the product.

    Foreign rows are judged too, in the one direction a concurrent lock cannot break:
    every row the query returned, own or not, is a row the tick would drive.
    """
    assert_reason_expectations_match_python(tuple(NON_RETRYABLE_REASON_PREFIXES))
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        try:
            population = _add_population(db, ledger, now)
            await db.flush()
            # Read before the select: a freeze is one-way, so a build row the select
            # returns was not frozen at this earlier read either.
            frozen = await _frozen_reservations(db)

            returned = await ledger.due(db, 1_000_000, CAP, now)

            assert_parity(population, {r.id for r in returned})
            not_drivable = [
                (r.id, r.intended, r.last_error)
                for r in returned
                if not python_would_drive(r, lambda rid: rid in frozen)
            ]
            assert not not_drivable, (
                f"the due query returned {len(not_drivable)} row(s) the tick would skip: "
                f"{not_drivable[:5]}"
            )
        finally:
            await db.rollback()


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
@pytest.mark.parametrize(
    "prefixes",
    [
        (),
        (*NON_RETRYABLE_REASON_PREFIXES, "a third pinned reason"),
        ("100%_", "\u00c9chec du pilote", "boom"),
        ("",),
    ],
    ids=["no_prefixes", "one_more_prefix", "wildcard_and_non_ascii_prefixes", "empty_prefix"],
)
async def test_due_query_follows_the_prefix_tuple(ledger, prefixes, session_factory, monkeypatch):
    """Parity on Postgres for other prefix tuples than today's two: one more prefix, a
    prefix holding the LIKE wildcards `%` and `_` and one outside ASCII, no prefix, and
    the empty prefix. The SQL is built from the tuple the Python rule reads, so the two
    move together; a prefix test written with LIKE, or a prefix list kept in the SQL,
    fails here.
    """
    monkeypatch.setattr(nats_consumer, "NON_RETRYABLE_REASON_PREFIXES", prefixes)
    monkeypatch.setattr(wiring_retry_service, "_NON_RETRYABLE_PREFIXES", prefixes)
    assert_reason_expectations_match_python(prefixes)
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        try:
            population = _add_population(db, ledger, now, prefixes)
            await db.flush()

            returned = await ledger.due(db, 1_000_000, CAP, now)

            assert_parity(population, {r.id for r in returned})
        finally:
            await db.rollback()


# --- Starvation, with the LIMIT applied ---------------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_stale_rows_cannot_fill_the_batch(ledger, session_factory):
    """A batch of not-retryable rows and a batch of frozen build rows, all older than
    one retryable row and older than anything else in the ledger: the real query with
    the batch LIMIT returns the retryable row first and none of the stale ones.

    Then the same for the ledger's own history: a second retryable row, newest in the
    table, is returned behind drivable rows only, so a LIMIT one past its position
    reaches it however many stale rows of other reservations sit older than it.
    """
    batch = 20
    frozen_reservation, stale_reservation, live_reservation = (uuid.uuid4() for _ in range(3))
    _own(frozen_reservation, stale_reservation, live_reservation)
    stale = [
        ledger.make(
            stale_reservation,
            i,
            intended=BUILD if i % 2 else RELEASE,
            attempts=0,
            last_error=f"{NON_RETRYABLE_REASON_PREFIXES[i % 2]}: switch gone",
            created_at=ANCIENT + timedelta(seconds=i),
        )
        for i in range(batch)
    ] + [
        ledger.make(
            frozen_reservation,
            1000 + i,
            intended=BUILD,
            attempts=1,
            last_error="boom",
            created_at=ANCIENT + timedelta(seconds=1000 + i),
        )
        for i in range(batch)
    ]
    oldest_retryable = ledger.make(
        live_reservation,
        5000,
        intended=BUILD,
        attempts=1,
        last_error="boom",
        created_at=ANCIENT + timedelta(days=1),
    )
    newest_retryable = ledger.make(
        live_reservation,
        5001,
        intended=RELEASE,
        attempts=1,
        last_error=None,
        created_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    async with session_factory() as db:
        try:
            db.add(ReservationWiringState(reservation_id=frozen_reservation, frozen=True))
            db.add_all([newest_retryable, oldest_retryable, *reversed(stale)])
            await db.flush()
            stale_ids = {row.id for row in stale}
            frozen = await _frozen_reservations(db)

            first_batch = await ledger.due(db, batch, CAP)

            assert first_batch, "the batch must not be empty"
            assert first_batch[0].id == oldest_retryable.id, (
                "the oldest drivable row of the ledger must lead the batch; "
                f"got a row with last_error={first_batch[0].last_error!r} "
                f"intended={first_batch[0].intended}"
            )
            assert not stale_ids & {r.id for r in first_batch}

            everything = await ledger.due(db, 1_000_000, CAP)
            assert newest_retryable.id in {r.id for r in everything}, (
                "a retryable row with no recorded reason must be returned"
            )
            position = [r.id for r in everything].index(newest_retryable.id)
            ahead = everything[:position]
            assert all(python_would_drive(r, lambda rid: rid in frozen) for r in ahead), (
                "a row the tick would skip sits ahead of the newest retryable row"
            )
            exactly_enough = await ledger.due(db, position + 1, CAP)
            assert exactly_enough[-1].id == newest_retryable.id, (
                "a LIMIT one past the drivable rows ahead of it must reach the row"
            )
        finally:
            await db.rollback()


# --- The excluded counts ------------------------------------------------------


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_count_tick_excluded_counts_the_case_product(ledger, session_factory):
    """`count_tick_excluded` on real Postgres: adding the case product raises the two
    counts by exactly the product's not-retryable and frozen-build rows under the cap.

    REPEATABLE READ, so the count before and the count after see one snapshot of every
    other reservation's rows and the difference is this test's rows alone.
    """
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        try:
            await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            before = await wiring_due.count_tick_excluded(db, ledger.model, CAP)
            population = _add_population(db, ledger, now)
            await db.flush()

            after = await wiring_due.count_tick_excluded(db, ledger.model, CAP)

            under_cap = [c for c in population.cases_by_id.values() if not c.at_cap]
            not_retryable = sum(1 for c in under_cap if not c.retryable)
            frozen_build = sum(
                1 for c in under_cap if c.retryable and c.intended != RELEASE and c.frozen
            )
            assert not_retryable and frozen_build
            assert (after[0] - before[0], after[1] - before[1]) == (not_retryable, frozen_build)
        finally:
            await db.rollback()


# --- What the select locks ----------------------------------------------------


def _is_lock_not_available(exc: DBAPIError) -> bool:
    """SQLSTATE 55P03, what FOR UPDATE NOWAIT raises on a row another session holds."""
    return getattr(exc.orig, "sqlstate", None) == "55P03" or "LockNotAvailable" in repr(exc.orig)


@pytest.mark.parametrize("ledger", LEDGERS, ids=LEDGER_IDS)
async def test_due_select_locks_its_ledger_row_and_no_wiring_state_row(ledger, session_factory):
    """FOR UPDATE SKIP LOCKED with the correlated frozen test: Postgres accepts the
    statement, locks the selected LEDGER row, and locks nothing in
    reservation_wiring_state.

    The own row is a BUILD row of a reservation whose state row exists and is not
    frozen, so the frozen test really reads that state row to return the ledger row.
    While the selecting transaction is open, a second session can update and lock that
    state row at once, cannot lock the ledger row, and does not get the ledger row from
    its own run of the due query (SKIP LOCKED).
    """
    reservation_id = uuid.uuid4()
    _own(reservation_id)
    hidden_cap = HIDDEN_ATTEMPTS + 1
    row = ledger.make(
        reservation_id,
        1,
        intended=BUILD,
        attempts=HIDDEN_ATTEMPTS,
        last_error="boom",
        created_at=ANCIENT,
    )
    try:
        async with session_factory() as db:
            db.add(ReservationWiringState(reservation_id=reservation_id, frozen=False))
            db.add(row)
            await db.commit()

        async with session_factory() as holder, session_factory() as other:
            try:
                selected = await ledger.due(holder, 1, hidden_cap)
                assert [r.id for r in selected] == [row.id], (
                    "the own row is the oldest drivable row under this test's cap"
                )

                # The wiring-state row is free: an UPDATE of it returns at once (the
                # connection's lock_timeout turns a wait into a failure).
                updated = await other.execute(
                    update(ReservationWiringState)
                    .where(ReservationWiringState.reservation_id == reservation_id)
                    .values(last_applied_fork_version=7)
                )
                assert updated.rowcount == 1
                await other.commit()
                await other.execute(
                    select(ReservationWiringState.reservation_id)
                    .where(ReservationWiringState.reservation_id == reservation_id)
                    .with_for_update(nowait=True)
                )
                await other.rollback()

                # The ledger row is held by the selecting transaction.
                with pytest.raises(DBAPIError) as refused:
                    await other.execute(
                        select(ledger.model.id)
                        .where(ledger.model.id == row.id)
                        .with_for_update(nowait=True)
                    )
                assert _is_lock_not_available(refused.value), repr(refused.value.orig)
                await other.rollback()

                skipped = await ledger.due(other, 1_000_000, hidden_cap)
                assert row.id not in {r.id for r in skipped}, "SKIP LOCKED must skip a held row"
                await other.rollback()
            finally:
                await holder.rollback()

            # Released with the transaction: the row can be locked again.
            await other.execute(
                select(ledger.model.id)
                .where(ledger.model.id == row.id)
                .with_for_update(nowait=True)
            )
            await other.rollback()
    finally:
        async with session_factory() as db:
            for model in (ledger.model, ReservationWiringState):
                await db.execute(delete(model).where(model.reservation_id == reservation_id))
            await db.commit()
