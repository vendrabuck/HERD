"""Unit tests for the OIDC login flow store (ADR 0016 phase 2, D2 and D11).

In-memory SQLite. The status by operation matrix gives EVERY row the same full
set of secrets (state, binding, handoff, verifier, a user, unexpired times), so
the status predicate of each guarded UPDATE is the only thing that can refuse an
operation; the expiry and inactive-user variants then change one condition at a
time. Real concurrency (two sessions contending for one row) is proven on
Postgres in test_oidc_flow_service_live_pg.py; here the read-to-UPDATE window of
`claim_flow` is driven deterministically by interposing a competing claim.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base
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
from app.services import oidc_flow_service
from app.services.oidc_flow_service import (
    DEFAULT_NEXT_PATH,
    FLOW_TTL,
    HANDOFF_TTL,
    PRUNE_AFTER,
    PRUNE_BATCH,
    SECRET_MIN_LENGTH,
    ClaimedFlow,
    claim_flow,
    complete_flow,
    exchange_handoff,
    fail_flow,
    sanitize_next_path,
    start_flow,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

T0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
VERIFIER = "v" * 64
TINY = timedelta(microseconds=1)


def sec(tag: str) -> str:
    """A secret of realistic length (the store refuses one under 43 characters),
    distinct per tag."""
    return f"{tag}-" + "x" * SECRET_MIN_LENGTH


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock(T0)
    monkeypatch.setattr(oidc_flow_service, "_utcnow", c)
    return c


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def _user(db, *, active: bool = True, email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    db.add(
        User(
            id=uid,
            email=email or f"{uid.hex}@example.com",
            username=uid.hex,
            auth_source="oidc",
            role=Role.USER,
            is_active=active,
        )
    )
    await db.commit()
    return uid


class Secrets:
    """One row's raw secrets, unique per row."""

    def __init__(self) -> None:
        tag = uuid.uuid4().hex
        self.state = sec(f"state-{tag}")
        self.binding = sec(f"binding-{tag}")
        self.handoff = sec(f"handoff-{tag}")
        self.nonce = f"nonce-{tag}"


async def _insert_row(
    db,
    status: str,
    *,
    user_id: uuid.UUID,
    expires_at: datetime,
    handoff_expires_at: datetime,
    secrets: Secrets | None = None,
) -> tuple[uuid.UUID, Secrets]:
    s = secrets or Secrets()
    row = OidcLoginFlow(
        state_hash=_sha(s.state),
        binding_hash=_sha(s.binding),
        nonce=s.nonce,
        code_verifier=VERIFIER,
        next_path="/reservations",
        status=status,
        user_id=user_id,
        handoff_hash=_sha(s.handoff),
        expires_at=expires_at,
        handoff_expires_at=handoff_expires_at,
    )
    db.add(row)
    await db.commit()
    return row.id, s


async def _row(flow_id: uuid.UUID) -> OidcLoginFlow:
    async with TestSessionLocal() as db:
        return (
            await db.execute(select(OidcLoginFlow).where(OidcLoginFlow.id == flow_id))
        ).scalar_one()


async def _count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(OidcLoginFlow))).scalar_one()


# --- the status by operation matrix -------------------------------------------

OPERATIONS = ("claim", "complete", "fail", "exchange")

# (status, operation) to (succeeds, status afterwards), with every row live and
# every secret right, so only the status predicate decides.
LIVE_MATRIX = {
    (FLOW_PENDING, "claim"): (True, FLOW_CLAIMED),
    (FLOW_PENDING, "complete"): (False, FLOW_PENDING),
    (FLOW_PENDING, "fail"): (False, FLOW_PENDING),
    (FLOW_PENDING, "exchange"): (False, FLOW_PENDING),
    (FLOW_CLAIMED, "claim"): (False, FLOW_CLAIMED),
    (FLOW_CLAIMED, "complete"): (True, FLOW_AUTHENTICATED),
    (FLOW_CLAIMED, "fail"): (True, FLOW_FAILED),
    (FLOW_CLAIMED, "exchange"): (False, FLOW_CLAIMED),
    (FLOW_AUTHENTICATED, "claim"): (False, FLOW_AUTHENTICATED),
    (FLOW_AUTHENTICATED, "complete"): (False, FLOW_AUTHENTICATED),
    (FLOW_AUTHENTICATED, "fail"): (False, FLOW_AUTHENTICATED),
    (FLOW_AUTHENTICATED, "exchange"): (True, FLOW_EXCHANGED),
    (FLOW_EXCHANGED, "claim"): (False, FLOW_EXCHANGED),
    (FLOW_EXCHANGED, "complete"): (False, FLOW_EXCHANGED),
    (FLOW_EXCHANGED, "fail"): (False, FLOW_EXCHANGED),
    (FLOW_EXCHANGED, "exchange"): (False, FLOW_EXCHANGED),
    (FLOW_FAILED, "claim"): (False, FLOW_FAILED),
    (FLOW_FAILED, "complete"): (False, FLOW_FAILED),
    (FLOW_FAILED, "fail"): (False, FLOW_FAILED),
    (FLOW_FAILED, "exchange"): (False, FLOW_FAILED),
}

# The same with expires_at AND handoff_expires_at already past: only a failure
# stays recordable (fail_flow has no expiry guard, by design).
EXPIRED_MATRIX = {
    key: ((True, FLOW_FAILED) if key == (FLOW_CLAIMED, "fail") else (False, key[0]))
    for key in LIVE_MATRIX
}


def test_matrix_covers_every_status_and_operation():
    expected = {(s, op) for s in FLOW_STATUSES for op in OPERATIONS}
    assert set(LIVE_MATRIX) == expected
    assert set(EXPIRED_MATRIX) == expected


async def _apply(op: str, db, flow_id: uuid.UUID, s: Secrets, user_id: uuid.UUID):
    if op == "claim":
        return await claim_flow(db, state=s.state, binding_secret=s.binding)
    if op == "complete":
        return await complete_flow(db, flow_id, user_id=user_id, handoff_secret=s.handoff)
    if op == "fail":
        return await fail_flow(db, flow_id)
    return await exchange_handoff(db, handoff_secret=s.handoff)


def _succeeded(op: str, result) -> bool:
    if op in ("complete", "fail"):
        assert isinstance(result, bool)
        return result
    return result is not None


@pytest.mark.parametrize("status,op", sorted(LIVE_MATRIX))
async def test_status_by_operation_matrix_live_rows(clock, status, op):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            status,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        result = await _apply(op, db, flow_id, s, uid)
    want_ok, want_status = LIVE_MATRIX[(status, op)]
    assert _succeeded(op, result) is want_ok
    assert (await _row(flow_id)).status == want_status
    if op == "claim" and want_ok:
        assert result == ClaimedFlow(
            id=flow_id, nonce=s.nonce, code_verifier=VERIFIER, next_path="/reservations"
        )
    if op == "exchange" and want_ok:
        assert result == (uid, "/reservations")


@pytest.mark.parametrize("status,op", sorted(EXPIRED_MATRIX))
async def test_status_by_operation_matrix_expired_rows(clock, status, op):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            status,
            user_id=uid,
            expires_at=T0 - TINY,
            handoff_expires_at=T0 - TINY,
        )
        result = await _apply(op, db, flow_id, s, uid)
    want_ok, want_status = EXPIRED_MATRIX[(status, op)]
    assert _succeeded(op, result) is want_ok
    assert (await _row(flow_id)).status == want_status


@pytest.mark.parametrize("status", FLOW_STATUSES)
async def test_exchange_refused_for_an_inactive_user_in_every_status(clock, status):
    async with TestSessionLocal() as db:
        uid = await _user(db, active=False)
        flow_id, s = await _insert_row(
            db,
            status,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        assert await exchange_handoff(db, handoff_secret=s.handoff) is None
    assert (await _row(flow_id)).status == status


# --- secrets at rest -------------------------------------------------------------


def _all_column_text(row: OidcLoginFlow) -> list[str]:
    return [str(getattr(row, c.key)) for c in OidcLoginFlow.__table__.columns]


async def test_start_flow_stores_hashes_never_the_raw_state_or_binding(clock):
    async with TestSessionLocal() as db:
        flow_id = await start_flow(
            db,
            state=sec("raw-state-value"),
            nonce="raw-nonce-value",
            code_verifier=VERIFIER,
            binding_secret=sec("raw-binding-value"),
            next_path="/devices",
        )
    row = await _row(flow_id)
    assert row.state_hash == _sha(sec("raw-state-value"))
    assert row.binding_hash == _sha(sec("raw-binding-value"))
    stored = _all_column_text(row)
    for raw in (sec("raw-state-value"), sec("raw-binding-value"), "raw-state-value"):
        assert not any(raw in value for value in stored), raw
    # D2: the nonce is compared verbatim to the ID token, so it is stored as is;
    # the verifier must be sent to the token endpoint, so it is too, until claim.
    assert row.nonce == "raw-nonce-value"
    assert row.code_verifier == VERIFIER
    assert row.status == FLOW_PENDING
    assert row.next_path == "/devices"
    assert row.user_id is None and row.handoff_hash is None and row.handoff_expires_at is None
    assert _aware(row.expires_at) == T0 + FLOW_TTL


async def test_claim_nulls_the_verifier_and_complete_stores_only_the_handoff_hash(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id = await start_flow(
            db,
            state=sec("st"),
            nonce="no",
            code_verifier=VERIFIER,
            binding_secret=sec("bi"),
            next_path=None,
        )
        claimed = await claim_flow(db, state=sec("st"), binding_secret=sec("bi"))
        assert claimed is not None and claimed.code_verifier == VERIFIER
        row = await _row(flow_id)
        assert row.code_verifier is None
        assert not any(VERIFIER in value for value in _all_column_text(row))

        clock.now = T0 + timedelta(seconds=5)
        assert await complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("raw-handoff"))
    row = await _row(flow_id)
    assert row.handoff_hash == _sha(sec("raw-handoff"))
    assert not any(sec("raw-handoff") in value for value in _all_column_text(row))
    assert row.user_id == uid
    assert _aware(row.handoff_expires_at) == T0 + timedelta(seconds=5) + HANDOFF_TTL


async def test_fail_leaves_no_verifier_on_the_row(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, _ = await _insert_row(
            db,
            FLOW_CLAIMED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        assert await fail_flow(db, flow_id) is True
    assert (await _row(flow_id)).code_verifier is None


# --- claim: secrets, boundaries, and the read-to-UPDATE window -------------------


async def _pending(db, s: Secrets, expires_at: datetime) -> uuid.UUID:
    uid = await _user(db)
    flow_id, _ = await _insert_row(
        db,
        FLOW_PENDING,
        user_id=uid,
        expires_at=expires_at,
        handoff_expires_at=expires_at,
        secrets=s,
    )
    return flow_id


async def test_claim_with_a_wrong_binding_secret_leaves_the_row_pending_and_usable(clock):
    s = Secrets()
    async with TestSessionLocal() as db:
        flow_id = await _pending(db, s, T0 + FLOW_TTL)
        assert await claim_flow(db, state=s.state, binding_secret=sec("not-the-binding")) is None
        row = await _row(flow_id)
        assert row.status == FLOW_PENDING and row.code_verifier == VERIFIER
        assert (await claim_flow(db, state=s.state, binding_secret=s.binding)) is not None
    assert (await _row(flow_id)).status == FLOW_CLAIMED


async def test_claim_with_an_unknown_state_matches_nothing(clock):
    s = Secrets()
    async with TestSessionLocal() as db:
        flow_id = await _pending(db, s, T0 + FLOW_TTL)
        assert await claim_flow(db, state=sec("unknown"), binding_secret=s.binding) is None
    assert (await _row(flow_id)).status == FLOW_PENDING


async def test_claim_with_another_rows_binding_secret_is_refused(clock):
    a, b = Secrets(), Secrets()
    async with TestSessionLocal() as db:
        flow_a = await _pending(db, a, T0 + FLOW_TTL)
        flow_b = await _pending(db, b, T0 + FLOW_TTL)
        assert await claim_flow(db, state=a.state, binding_secret=b.binding) is None
    assert (await _row(flow_a)).status == FLOW_PENDING
    assert (await _row(flow_b)).status == FLOW_PENDING


@pytest.mark.parametrize(
    "offset,claimable",
    [(-timedelta(seconds=1), True), (-TINY, True), (timedelta(0), False), (TINY, False)],
    ids=["1s-before", "1us-before", "at-expiry", "1us-after"],
)
async def test_claim_expiry_boundary(clock, offset, claimable):
    s = Secrets()
    async with TestSessionLocal() as db:
        flow_id = await _pending(db, s, T0 + FLOW_TTL)
        clock.now = T0 + FLOW_TTL + offset
        result = await claim_flow(db, state=s.state, binding_secret=s.binding)
    assert (result is not None) is claimable
    assert (await _row(flow_id)).status == (FLOW_CLAIMED if claimable else FLOW_PENDING)


class _InterposingSession:
    """Runs `before` once, just before the wrapped session's first UPDATE."""

    def __init__(self, inner, before) -> None:
        self._inner = inner
        self._before = before
        self.fired = False

    async def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Update) and not self.fired:
            self.fired = True
            await self._before()
        return await self._inner.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_claim_loses_to_a_claim_that_lands_between_its_read_and_its_update(clock):
    s = Secrets()
    results: dict[str, object] = {}
    async with TestSessionLocal() as db:
        flow_id = await _pending(db, s, T0 + FLOW_TTL)

        async def competitor():
            async with TestSessionLocal() as other:
                results["b"] = await claim_flow(other, state=s.state, binding_secret=s.binding)

        wrapped = _InterposingSession(db, competitor)
        results["a"] = await claim_flow(wrapped, state=s.state, binding_secret=s.binding)
    assert wrapped.fired, "the competing claim never ran; the window was not exercised"
    assert results["b"] is not None
    assert results["a"] is None
    assert (await _row(flow_id)).status == FLOW_CLAIMED


# --- complete, fail, exchange ------------------------------------------------------


@pytest.mark.parametrize(
    "offset,completes",
    [(-TINY, True), (timedelta(0), False), (TINY, False)],
    ids=["1us-before", "at-expiry", "1us-after"],
)
async def test_complete_expiry_boundary(clock, offset, completes):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            FLOW_CLAIMED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + FLOW_TTL,
        )
        clock.now = T0 + FLOW_TTL + offset
        assert await complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("h")) is completes
    assert (await _row(flow_id)).status == (FLOW_AUTHENTICATED if completes else FLOW_CLAIMED)


async def test_complete_and_fail_exclude_each_other(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        first, _ = await _insert_row(
            db, FLOW_CLAIMED, user_id=uid, expires_at=T0 + FLOW_TTL, handoff_expires_at=T0
        )
        second, _ = await _insert_row(
            db, FLOW_CLAIMED, user_id=uid, expires_at=T0 + FLOW_TTL, handoff_expires_at=T0
        )
        assert await complete_flow(db, first, user_id=uid, handoff_secret=sec("h1")) is True
        assert await fail_flow(db, first) is False
        assert await fail_flow(db, second) is True
        assert await complete_flow(db, second, user_id=uid, handoff_secret=sec("h2")) is False
    assert (await _row(first)).status == FLOW_AUTHENTICATED
    assert (await _row(second)).status == FLOW_FAILED


async def test_complete_with_a_handoff_hash_another_row_holds_writes_nothing(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        held, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        claimed, _ = await _insert_row(
            db, FLOW_CLAIMED, user_id=uid, expires_at=T0 + FLOW_TTL, handoff_expires_at=T0
        )
        assert await complete_flow(db, claimed, user_id=uid, handoff_secret=s.handoff) is False
    assert (await _row(claimed)).status == FLOW_CLAIMED


async def test_complete_and_fail_on_an_unknown_id_match_nothing(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        assert await complete_flow(db, uuid.uuid4(), user_id=uid, handoff_secret=sec("h")) is False
        assert await fail_flow(db, uuid.uuid4()) is False


@pytest.mark.parametrize(
    "offset,exchangeable",
    [(-TINY, True), (timedelta(0), False), (TINY, False)],
    ids=["1us-before", "at-expiry", "1us-after"],
)
async def test_exchange_expiry_boundary(clock, offset, exchangeable):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        clock.now = T0 + HANDOFF_TTL + offset
        result = await exchange_handoff(db, handoff_secret=s.handoff)
    assert (result is not None) is exchangeable
    assert (await _row(flow_id)).status == (FLOW_EXCHANGED if exchangeable else FLOW_AUTHENTICATED)


async def test_second_exchange_of_one_handoff_gets_nothing(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        assert await exchange_handoff(db, handoff_secret=s.handoff) == (uid, "/reservations")
        assert await exchange_handoff(db, handoff_secret=s.handoff) is None
    assert (await _row(flow_id)).status == FLOW_EXCHANGED


async def test_exchange_with_a_wrong_handoff_secret_leaves_the_row_authenticated(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        assert await exchange_handoff(db, handoff_secret=sec("wrong")) is None
        assert await exchange_handoff(db, handoff_secret=s.handoff) is not None


async def test_exchange_refused_when_the_user_is_deactivated_after_the_callback(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        user.is_active = False
        await db.commit()
        assert await exchange_handoff(db, handoff_secret=s.handoff) is None
    assert (await _row(flow_id)).status == FLOW_AUTHENTICATED


async def test_exchange_checks_its_own_rows_user_not_any_active_user(clock):
    """The active-user subquery is correlated to the row: an active OTHER user
    must not let an inactive row user through."""
    async with TestSessionLocal() as db:
        await _user(db, active=True)
        inactive = await _user(db, active=False)
        flow_id, s = await _insert_row(
            db,
            FLOW_AUTHENTICATED,
            user_id=inactive,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + HANDOFF_TTL,
        )
        assert await exchange_handoff(db, handoff_secret=s.handoff) is None
    assert (await _row(flow_id)).status == FLOW_AUTHENTICATED


async def test_full_flow_from_start_to_exchange(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id = await start_flow(
            db,
            state=sec("s"),
            nonce="n",
            code_verifier=VERIFIER,
            binding_secret=sec("b"),
            next_path="/reservations?tab=mine",
        )
        clock.now = T0 + timedelta(seconds=30)
        claimed = await claim_flow(db, state=sec("s"), binding_secret=sec("b"))
        assert claimed == ClaimedFlow(
            id=flow_id, nonce="n", code_verifier=VERIFIER, next_path="/reservations?tab=mine"
        )
        assert await complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("h")) is True
        clock.now = T0 + timedelta(seconds=40)
        assert await exchange_handoff(db, handoff_secret=sec("h")) == (
            uid,
            "/reservations?tab=mine",
        )
    assert (await _row(flow_id)).status == FLOW_EXCHANGED


# --- start_flow: input checks and pruning -------------------------------------------


@pytest.mark.parametrize(
    "field", ["state", "nonce", "binding_secret"], ids=["state", "nonce", "binding"]
)
async def test_start_flow_refuses_an_empty_secret(clock, field):
    kwargs = dict(
        state=sec("s"), nonce="n", code_verifier=VERIFIER, binding_secret=sec("b"), next_path="/"
    )
    kwargs[field] = ""
    async with TestSessionLocal() as db:
        with pytest.raises(ValueError):
            await start_flow(db, **kwargs)
    assert await _count() == 0


@pytest.mark.parametrize("length,ok", [(42, False), (43, True), (128, True), (129, False)])
async def test_start_flow_verifier_length_bounds(clock, length, ok):
    async with TestSessionLocal() as db:
        call = start_flow(
            db,
            state=sec("s"),
            nonce="n",
            code_verifier="a" * length,
            binding_secret=sec("b"),
            next_path="/",
        )
        if ok:
            await call
        else:
            with pytest.raises(ValueError):
                await call
    assert await _count() == (1 if ok else 0)


async def test_start_flow_refuses_a_reused_state(clock):
    async with TestSessionLocal() as db:
        await start_flow(
            db,
            state=sec("s"),
            nonce="n",
            code_verifier=VERIFIER,
            binding_secret=sec("b"),
            next_path="/",
        )
        with pytest.raises(IntegrityError):
            await start_flow(
                db,
                state=sec("s"),
                nonce="n2",
                code_verifier=VERIFIER,
                binding_secret=sec("b2"),
                next_path="/",
            )
    assert await _count() == 1


async def _seed_at(expires_at: datetime, status: str) -> uuid.UUID:
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, _ = await _insert_row(
            db, status, user_id=uid, expires_at=expires_at, handoff_expires_at=expires_at
        )
        return flow_id


async def _start(db, state: str = "new-state") -> uuid.UUID:
    return await start_flow(
        db,
        state=sec(state),
        nonce="n",
        code_verifier=VERIFIER,
        binding_secret=sec("b"),
        next_path="/",
    )


@pytest.mark.parametrize("status", FLOW_STATUSES)
@pytest.mark.parametrize(
    "age,pruned",
    [
        (PRUNE_AFTER + timedelta(days=30), True),
        (PRUNE_AFTER + TINY, True),
        (PRUNE_AFTER, False),
        (PRUNE_AFTER - TINY, False),
        (timedelta(seconds=1), False),
        (-FLOW_TTL, False),
    ],
    ids=["30-days-past", "1us-outside", "at-boundary", "1us-inside", "just-expired", "live"],
)
async def test_prune_boundary_by_status(clock, status, age, pruned):
    """A row is pruned only when its expires_at is MORE than one hour past, in
    any status; a row at or inside the window, and every live row, stays."""
    flow_id = await _seed_at(T0 - age, status)
    async with TestSessionLocal() as db:
        await _start(db)
    async with TestSessionLocal() as db:
        remaining = (
            await db.execute(select(OidcLoginFlow.id).where(OidcLoginFlow.id == flow_id))
        ).scalar_one_or_none()
    assert (remaining is None) is pruned
    assert await _count() == (1 if pruned else 2)


async def test_prune_deletes_at_most_one_batch_per_start(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        old = T0 - PRUNE_AFTER - timedelta(minutes=1)
        for _ in range(PRUNE_BATCH + 3):
            await _insert_row(db, FLOW_FAILED, user_id=uid, expires_at=old, handoff_expires_at=old)
        live, _ = await _insert_row(
            db,
            FLOW_PENDING,
            user_id=uid,
            expires_at=T0 + FLOW_TTL,
            handoff_expires_at=T0 + FLOW_TTL,
        )
        await _start(db, "first")
        # 3 prunable left, the live row, and the new one.
        assert await _count() == 5
        await _start(db, "second")
        assert await _count() == 3
    assert (await _row(live)).status == FLOW_PENDING


async def test_prune_commits_in_its_own_transaction_before_the_insert(clock):
    """A failed insert (here a reused state) does not undo the prune."""
    old = await _seed_at(T0 - PRUNE_AFTER - timedelta(minutes=1), FLOW_EXCHANGED)
    async with TestSessionLocal() as db:
        await _start(db, "dup")
    clock.now = T0 + timedelta(seconds=1)
    other_old = await _seed_at(T0 - PRUNE_AFTER - timedelta(minutes=1), FLOW_FAILED)
    async with TestSessionLocal() as db:
        with pytest.raises(IntegrityError):
            await _start(db, "dup")
    async with TestSessionLocal() as db:
        ids = set((await db.execute(select(OidcLoginFlow.id))).scalars().all())
    assert old not in ids and other_old not in ids
    assert len(ids) == 1


async def test_a_live_handoff_can_never_be_prunable(clock):
    """complete_flow refuses after expires_at, so the latest possible handoff
    expiry (a completion 1 microsecond before expires_at) is still far inside the
    prune window: the prune rule cannot reach a live handoff."""
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, _ = await _insert_row(
            db, FLOW_CLAIMED, user_id=uid, expires_at=T0 + FLOW_TTL, handoff_expires_at=T0
        )
        clock.now = T0 + FLOW_TTL - TINY
        assert await complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("late")) is True
    row = await _row(flow_id)
    assert _aware(row.handoff_expires_at) < _aware(row.expires_at) + PRUNE_AFTER
    clock.now = _aware(row.handoff_expires_at) - TINY
    async with TestSessionLocal() as db:
        await _start(db)
    assert (await _row(flow_id)).status == FLOW_AUTHENTICATED


# --- next_path (D11) -------------------------------------------------------------------

ACCEPTED_PATHS = [
    "/",
    "/topology",
    "/reservations/123",
    "/reservations?tab=mine&page=2",
    "/devices#ports",
    "/a/b/c/../d",
    "/path:with:colons",
    "/%2F%2Fevil.example",
    "/café",
    "/" + "a" * 511,
]

REFUSED_PATHS = [
    None,
    "",
    "topology",
    "./topology",
    "//evil.example",
    "//evil.example/path",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "/a\\b",
    "https://evil.example",
    "http:/evil.example",
    "javascript:alert(1)",
    " /topology",
    "/topology\n",
    "/top\rology",
    "/top\tology",
    "/top\x00ology",
    "/top\x7fology",
    "/top\x85ology",
    "/" + "a" * 512,
]


@pytest.mark.parametrize("path", ACCEPTED_PATHS)
def test_next_path_accepted(path):
    assert sanitize_next_path(path) == path


@pytest.mark.parametrize("path", REFUSED_PATHS)
def test_next_path_replaced_by_topology(path):
    assert sanitize_next_path(path) == DEFAULT_NEXT_PATH == "/topology"


async def test_start_flow_stores_the_sanitized_next_path(clock):
    async with TestSessionLocal() as db:
        flow_id = await start_flow(
            db,
            state=sec("s"),
            nonce="n",
            code_verifier=VERIFIER,
            binding_secret=sec("b"),
            next_path="//evil.example",
        )
    assert (await _row(flow_id)).next_path == "/topology"


# --- log hygiene (D9) ---------------------------------------------------------------


async def test_no_flow_operation_logs_a_secret(clock, caplog):
    secrets = {
        "state": "LOGSTATE-0123456789" + "z" * 40,
        "binding": "LOGBINDING-0123456789" + "z" * 40,
        "nonce": "LOGNONCE-0123456789",
        "verifier": "LOGVERIFIER-" + "x" * 40,
        "handoff": "LOGHANDOFF-0123456789" + "z" * 40,
    }
    with caplog.at_level(logging.DEBUG, logger="app"):
        async with TestSessionLocal() as db:
            uid = await _user(db)
            flow_id = await start_flow(
                db,
                state=secrets["state"],
                nonce=secrets["nonce"],
                code_verifier=secrets["verifier"],
                binding_secret=secrets["binding"],
                next_path="/",
            )
            await claim_flow(db, state=secrets["state"], binding_secret=sec("wrong"))
            await claim_flow(db, state=sec("unknown"), binding_secret=secrets["binding"])
            await claim_flow(db, state=secrets["state"], binding_secret=secrets["binding"])
            await claim_flow(db, state=secrets["state"], binding_secret=secrets["binding"])
            await complete_flow(db, flow_id, user_id=uid, handoff_secret=secrets["handoff"])
            await complete_flow(db, flow_id, user_id=uid, handoff_secret=secrets["handoff"])
            await exchange_handoff(db, handoff_secret=sec("wrong"))
            await exchange_handoff(db, handoff_secret=secrets["handoff"])
            await exchange_handoff(db, handoff_secret=secrets["handoff"])
            await fail_flow(db, flow_id)
    # The service's own loggers; the database driver's DEBUG statement log (off
    # in production) carries bound parameters and is not this module's output.
    records = [r for r in caplog.records if r.name.startswith("app.")]
    assert records, "expected the refusals to log"
    for record in records:
        text = record.getMessage() + " " + " ".join(str(v) for v in record.__dict__.values())
        for name, value in secrets.items():
            assert value not in text, f"{name} leaked into a log record"
            assert _sha(value) not in text, f"the {name} hash leaked into a log record"


# --- review round: secret lengths, non-string next, the best-effort prune ----------


@pytest.mark.parametrize("field", ["state", "binding_secret"])
@pytest.mark.parametrize(
    "value,ok",
    [("a" * 42, False), ("a" * 43, True), ("", False), (None, False)],
    ids=["42", "43", "empty", "none"],
)
async def test_start_flow_minimum_secret_length(clock, field, value, ok):
    kwargs = dict(
        state=sec("s"), nonce="n", code_verifier=VERIFIER, binding_secret=sec("b"), next_path="/"
    )
    kwargs[field] = value
    async with TestSessionLocal() as db:
        if ok:
            await start_flow(db, **kwargs)
        else:
            with pytest.raises(ValueError):
                await start_flow(db, **kwargs)
    assert await _count() == (1 if ok else 0)


@pytest.mark.parametrize(
    "handoff,ok",
    [("", False), ("h", False), ("a" * 42, False), (None, False), ("a" * 43, True)],
    ids=["empty", "1", "42", "none", "43"],
)
async def test_complete_flow_minimum_handoff_length(clock, handoff, ok):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id, _ = await _insert_row(
            db, FLOW_CLAIMED, user_id=uid, expires_at=T0 + FLOW_TTL, handoff_expires_at=T0
        )
        if ok:
            assert await complete_flow(db, flow_id, user_id=uid, handoff_secret=handoff) is True
        else:
            with pytest.raises(ValueError):
                await complete_flow(db, flow_id, user_id=uid, handoff_secret=handoff)
    assert (await _row(flow_id)).status == (FLOW_AUTHENTICATED if ok else FLOW_CLAIMED)


@pytest.mark.parametrize(
    "raw", [123, b"/topology", ["/devices"], ("/x",), object()], ids=lambda v: type(v).__name__
)
def test_next_path_non_string_is_replaced_not_raised(raw):
    assert sanitize_next_path(raw) == DEFAULT_NEXT_PATH


async def test_prune_failure_never_fails_the_begin(clock, caplog):
    """The prune is best effort: a database error in it is rolled back, logged
    once by class name, and the flow is still created."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.sql.dml import Delete

    old = await _seed_at(T0 - PRUNE_AFTER - timedelta(minutes=1), FLOW_FAILED)

    async def boom():
        raise OperationalError("DELETE FROM oidc_login_flows", {}, Exception("deadlock"))

    class _FailingDelete:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Delete):
                await boom()
            return await self._inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with caplog.at_level(logging.WARNING, logger="app.services.oidc_flow_service"):
        async with TestSessionLocal() as db:
            flow_id = await _start(_FailingDelete(db), "after-a-failed-prune")
    assert (await _row(flow_id)).status == FLOW_PENDING
    assert (await _row(old)).status == FLOW_FAILED, "the failed prune must delete nothing"
    records = [r for r in caplog.records if getattr(r, "action", None) == "oidc_flow_prune_failed"]
    assert len(records) == 1
    assert records[0].error_class == "OperationalError"
    assert "deadlock" not in records[0].getMessage()


async def test_prune_selects_its_batch_for_update_skip_locked(clock):
    """On Postgres the batch must skip rows another prune holds (no waiting, no
    deadlock between concurrent begins); pinned on the compiled statement."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql.dml import Delete

    seen = []

    class _Recorder:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Delete):
                seen.append(str(statement.compile(dialect=postgresql.dialect())))
            return await self._inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    async with TestSessionLocal() as db:
        await _start(_Recorder(db))
    [sql] = seen
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT" in sql


# --- the session contract (IAM-OIDC-14) -------------------------------------------------


async def test_every_flow_function_ends_its_transaction_on_every_outcome(clock):
    async with TestSessionLocal() as db:
        uid = await _user(db)
        flow_id = await _start(db, "contract")
        assert not db.in_transaction()
        steps = [
            lambda: claim_flow(db, state="no-such-state", binding_secret=sec("b")),
            lambda: claim_flow(db, state=sec("contract"), binding_secret=sec("wrong")),
            lambda: claim_flow(db, state=sec("contract"), binding_secret=sec("b")),
            lambda: claim_flow(db, state=sec("contract"), binding_secret=sec("b")),
            lambda: fail_flow(db, uuid.uuid4()),
            lambda: complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("contract-h")),
            lambda: complete_flow(db, flow_id, user_id=uid, handoff_secret=sec("contract-h")),
            lambda: exchange_handoff(db, handoff_secret=sec("wrong")),
            lambda: exchange_handoff(db, handoff_secret=sec("contract-h")),
            lambda: exchange_handoff(db, handoff_secret=sec("contract-h")),
        ]
        for step in steps:
            await step()
            assert not db.in_transaction()


async def test_start_flow_commits_work_its_caller_left_pending(clock):
    async with TestSessionLocal() as db:
        db.add(User(email="pending@example.com", username="pending", auth_source="local"))
        await _start(db, "commits-pending")
    async with TestSessionLocal() as other:
        found = await other.execute(select(User.id).where(User.username == "pending"))
        assert found.scalar_one_or_none() is not None
