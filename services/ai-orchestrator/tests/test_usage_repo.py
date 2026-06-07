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
