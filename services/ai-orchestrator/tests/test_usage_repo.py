"""Unit tests for the per-user daily token quota repo and helpers.

Covers the acceptance-criteria paths from issue #30: disabled-by-default
(no rows written, no enforcement), under/at/over-limit enforcement with the
structured 429 body, the chars/4 estimate fallback when the provider reports
zero usage, the implicit UTC-day reset (a different usage_date is a separate
total), and the status read.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app import config as config_module
from app.database import Base, engine
from app.models.ai_usage import AIUsage
from app.services import usage_repo
from app.services.llm_provider import Usage
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def set_quota(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", value)

    return _set


USER = uuid.uuid4()


# --- get_today_total + add_tokens (the upsert) ---


async def test_get_today_total_zero_when_no_row():
    async with TestSessionLocal() as db:
        assert await usage_repo.get_today_total(db, USER) == 0


async def test_add_tokens_inserts_then_increments():
    """Two add_tokens calls accumulate via the ON CONFLICT upsert (sqlite path)."""
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=10, output_tokens=5)
        assert await usage_repo.get_today_total(db, USER) == 15
        await usage_repo.add_tokens(db, USER, input_tokens=3, output_tokens=2)
        assert await usage_repo.get_today_total(db, USER) == 20


async def test_add_tokens_is_per_user():
    other = uuid.uuid4()
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=10, output_tokens=0)
        assert await usage_repo.get_today_total(db, other) == 0


# --- cache-token columns (issue #65): persisted, but excluded from the quota total ---


async def test_add_tokens_persists_cache_columns():
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(
            db,
            USER,
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=4096,
            cache_read_input_tokens=2048,
        )
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
    assert row.input_tokens == 10
    assert row.output_tokens == 5
    assert row.cache_creation_input_tokens == 4096
    assert row.cache_read_input_tokens == 2048


async def test_add_tokens_upserts_cache_columns():
    """A second add_tokens accumulates the cache counters via ON CONFLICT, just
    like input/output."""
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(
            db,
            USER,
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=200,
        )
        await usage_repo.add_tokens(
            db,
            USER,
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=20,
        )
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
    assert row.cache_creation_input_tokens == 110
    assert row.cache_read_input_tokens == 220


async def test_cache_tokens_excluded_from_quota_total():
    """Cache reads/writes are observability only: get_today_total sums input +
    output and must ignore the cache columns entirely."""
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(
            db,
            USER,
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=9_000,
            cache_read_input_tokens=9_000,
        )
        # 10 + 5 only; the 18_000 cache tokens are not metered.
        assert await usage_repo.get_today_total(db, USER) == 15


async def test_add_tokens_cache_columns_default_zero():
    """Omitting the cache kwargs (the openai_compat path) leaves them at 0."""
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=7, output_tokens=3)
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
    assert row.cache_creation_input_tokens == 0
    assert row.cache_read_input_tokens == 0


async def test_record_usage_persists_cache_columns(set_quota):
    """record_usage passes the provider-reported cache counts through; the quota
    total still reflects only input + output."""
    set_quota(1000)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db,
            USER,
            Usage(
                input_tokens=30,
                output_tokens=12,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=700,
            ),
        )
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
        assert await usage_repo.get_today_total(db, USER) == 42
    assert row.cache_creation_input_tokens == 500
    assert row.cache_read_input_tokens == 700


async def test_enforce_quota_ignores_cache_tokens(set_quota):
    """A user whose only large numbers are cache tokens is never blocked: the
    gate compares the input+output total, not cache activity."""
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(
            db,
            USER,
            input_tokens=10,
            output_tokens=10,
            cache_creation_input_tokens=10_000,
            cache_read_input_tokens=10_000,
        )
        await usage_repo.enforce_quota(db, USER)  # 20 < 100, must not raise


# --- enforce_quota ---


async def test_enforce_quota_disabled_is_noop_even_over_usage(set_quota):
    set_quota(0)
    async with TestSessionLocal() as db:
        # Even with usage present, a disabled quota never raises and writes nothing.
        await usage_repo.add_tokens(db, USER, input_tokens=10_000, output_tokens=0)
        await usage_repo.enforce_quota(db, USER)  # must not raise


async def test_record_usage_disabled_writes_no_row(set_quota):
    set_quota(0)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(db, USER, Usage(input_tokens=50, output_tokens=50))
        assert await usage_repo.get_today_total(db, USER) == 0


async def test_enforce_quota_under_limit_allows(set_quota):
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=99, output_tokens=0)
        await usage_repo.enforce_quota(db, USER)  # 99 < 100, must not raise


async def test_enforce_quota_at_limit_rejects_with_structured_body(set_quota):
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=60, output_tokens=40)  # exactly 100
        with pytest.raises(HTTPException) as exc:
            await usage_repo.enforce_quota(db, USER)
    assert exc.value.status_code == 429
    detail = exc.value.detail
    assert detail["limit"] == 100
    assert detail["used"] == 100
    assert detail["remaining"] == 0
    # reset_at is the next UTC midnight, ISO formatted.
    reset = datetime.fromisoformat(detail["reset_at"])
    assert reset.tzinfo is not None
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)


async def test_enforce_quota_just_under_limit_allows_boundary(set_quota):
    """used == limit - 1 is allowed; the boundary call may overshoot, the next is blocked."""
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=99, output_tokens=0)
        await usage_repo.enforce_quota(db, USER)  # allowed
        # Simulate that call overshooting, then the next enforce rejects.
        await usage_repo.add_tokens(db, USER, input_tokens=5, output_tokens=0)  # now 104
        with pytest.raises(HTTPException):
            await usage_repo.enforce_quota(db, USER)


# --- record_usage + fallback estimate ---


async def test_record_usage_books_provider_tokens(set_quota):
    set_quota(1000)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(db, USER, Usage(input_tokens=30, output_tokens=12))
        assert await usage_repo.get_today_total(db, USER) == 42


async def test_record_usage_falls_back_to_chars_over_four(set_quota):
    set_quota(1000)
    text = "x" * 40  # 40 chars -> 10 estimated tokens
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db, USER, Usage(input_tokens=0, output_tokens=0), fallback_text=text
        )
        assert await usage_repo.get_today_total(db, USER) == 10


async def test_record_usage_fallback_minimum_one(set_quota):
    set_quota(1000)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db, USER, Usage(input_tokens=0, output_tokens=0), fallback_text=""
        )
        # max(1, 0 // 4) == 1
        assert await usage_repo.get_today_total(db, USER) == 1


async def test_record_usage_asymmetric_zero_output_does_not_trigger_fallback(set_quota):
    """Only the BOTH-zero case triggers the chars/4 fallback. With a nonzero
    input and a zero output the provider numbers are booked verbatim: the row
    holds input=30, output=0, and the fallback estimate over fallback_text is
    never consulted."""
    set_quota(1000)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db,
            USER,
            Usage(input_tokens=30, output_tokens=0),
            # A nonzero fallback_text proves the estimate did not fire: if the
            # both-zero guard had triggered, output would be 10, not 0.
            fallback_text="x" * 40,
        )
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
        assert row.input_tokens == 30
        assert row.output_tokens == 0
        # Quota total reflects the verbatim provider numbers (30 + 0).
        assert await usage_repo.get_today_total(db, USER) == 30


async def test_record_usage_asymmetric_zero_input_does_not_trigger_fallback(set_quota):
    """Mirror of the above with the zero on the input side: output is nonzero,
    so the both-zero fallback stays dormant and input=0, output=12 are booked
    as reported."""
    set_quota(1000)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db,
            USER,
            Usage(input_tokens=0, output_tokens=12),
            fallback_text="x" * 40,
        )
        row = (await db.execute(AIUsage.__table__.select().where(AIUsage.user_id == USER))).first()
        assert row.input_tokens == 0
        assert row.output_tokens == 12
        assert await usage_repo.get_today_total(db, USER) == 12


# --- status + UTC-day reset ---


async def test_get_status_disabled():
    async with TestSessionLocal() as db:
        status = await usage_repo.get_status(db, USER)
    assert status.enabled is False
    assert status.limit == 0
    assert status.remaining == 0


async def test_get_status_reports_used_and_remaining(set_quota):
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=30, output_tokens=0)
        status = await usage_repo.get_status(db, USER)
    assert status.enabled is True
    assert status.limit == 100
    assert status.used == 30
    assert status.remaining == 70
    assert status.reset_at == datetime.combine(
        _utc_today() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )


async def test_get_status_remaining_floors_at_zero_when_over(set_quota):
    set_quota(100)
    async with TestSessionLocal() as db:
        await usage_repo.add_tokens(db, USER, input_tokens=150, output_tokens=0)
        status = await usage_repo.get_status(db, USER)
    assert status.used == 150
    assert status.remaining == 0


async def test_prior_day_usage_is_a_separate_total():
    """A row on a previous usage_date is ignored by today's total (UTC reset)."""
    yesterday = _utc_today() - timedelta(days=1)
    async with TestSessionLocal() as db:
        db.add(
            AIUsage(
                user_id=USER,
                usage_date=yesterday,
                input_tokens=500,
                output_tokens=500,
            )
        )
        await db.commit()
        assert await usage_repo.get_today_total(db, USER) == 0


def _utc_today() -> date:
    return datetime.now(UTC).date()
