"""Per-user daily AI token quota: accounting, enforcement, and status.

This module owns every read and write of the `ai_usage` table plus the two
hooks the AI routes call:

- `enforce_quota(db, user_id)` runs BEFORE a billable provider call. It is a
  no-op when the quota is disabled, and raises HTTP 429 when the user's
  accumulated tokens for the current UTC day already meet or exceed the limit,
  so the provider is never called in that case. The check is against the
  running total, not a prediction of the pending call's cost, so a user under
  the limit is always allowed one more call (which may overshoot); the next
  call is then rejected.
- `record_usage(db, user_id, usage, ...)` runs AFTER the call returns, adding
  the provider-reported tokens, or a chars/4 estimate when the provider omits
  usage (some openai_compat backends do).

With `settings.ai_daily_token_quota == 0` both hooks short-circuit and no rows
are written, so a deployment that has not opted in sees zero behavior change.
Usage lives entirely in the ai_orchestrator schema; there are no cross-schema
reads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_usage import AIUsage
from app.services.llm_provider import Usage


@dataclass(frozen=True)
class QuotaStatus:
    enabled: bool
    limit: int
    used: int
    remaining: int
    reset_at: datetime


@dataclass(frozen=True)
class UsageRow:
    """One (user_id, usage_date) row of per-user daily AI token usage, for the
    admin reporting view. Carries both the quota-bearing input/output counts and
    the observability-only cache counters."""

    user_id: uuid.UUID
    usage_date: date
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _next_utc_midnight(today: date) -> datetime:
    """First instant of the next UTC day: the reset_at the client is shown."""
    return datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)


async def get_today_total(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Sum of input+output tokens for (user_id, UTC today). 0 when no row."""
    stmt = select(
        func.coalesce(AIUsage.input_tokens, 0) + func.coalesce(AIUsage.output_tokens, 0)
    ).where(AIUsage.user_id == user_id, AIUsage.usage_date == _utc_today())
    result = await db.execute(stmt)
    return result.scalar_one_or_none() or 0


async def add_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> None:
    """Atomically add tokens to (user_id, UTC today), inserting on first use.

    ON CONFLICT against the unique (user_id, usage_date) index makes concurrent
    increments safe without a read-modify-write round trip. The dialect is
    sniffed from the bound engine so the same code path works on Postgres
    (asyncpg) in prod and SQLite (aiosqlite) in tests.

    The two cache counters are accumulated for observability only; they are NOT
    part of the quota total (get_today_total sums input + output). They default
    to 0 so the openai_compat path, which has no prompt caching, is unaffected.
    """
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert(AIUsage).values(
        user_id=user_id,
        usage_date=_utc_today(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "usage_date"],
        set_={
            "input_tokens": AIUsage.input_tokens + stmt.excluded.input_tokens,
            "output_tokens": AIUsage.output_tokens + stmt.excluded.output_tokens,
            "cache_creation_input_tokens": (
                AIUsage.cache_creation_input_tokens + stmt.excluded.cache_creation_input_tokens
            ),
            "cache_read_input_tokens": (
                AIUsage.cache_read_input_tokens + stmt.excluded.cache_read_input_tokens
            ),
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()


async def get_status(db: AsyncSession, user_id: uuid.UUID) -> QuotaStatus:
    """The caller's quota state for the current UTC day."""
    limit = settings.ai_daily_token_quota
    enabled = limit > 0
    used = await get_today_total(db, user_id)
    remaining = max(0, limit - used) if enabled else 0
    return QuotaStatus(
        enabled=enabled,
        limit=limit,
        used=used,
        remaining=remaining,
        reset_at=_next_utc_midnight(_utc_today()),
    )


async def query_usage(
    db: AsyncSession,
    *,
    start: date,
    end: date,
) -> list[UsageRow]:
    """Per-user daily AI token usage across [start, end] inclusive.

    Drives the admin reporting view (GET /api/ai/usage). The table holds exactly
    one row per (user_id, usage_date) thanks to the unique index, so this is a
    range-filtered, deterministically ordered read rather than a GROUP BY. Both
    bounds are inclusive UTC dates; an empty or inverted range yields []. Ordered
    by usage_date then user_id for a stable response.
    """
    stmt = (
        select(
            AIUsage.user_id,
            AIUsage.usage_date,
            AIUsage.input_tokens,
            AIUsage.output_tokens,
            AIUsage.cache_creation_input_tokens,
            AIUsage.cache_read_input_tokens,
        )
        .where(AIUsage.usage_date >= start, AIUsage.usage_date <= end)
        .order_by(AIUsage.usage_date, AIUsage.user_id)
    )
    result = await db.execute(stmt)
    return [
        UsageRow(
            user_id=row.user_id,
            usage_date=row.usage_date,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_creation_input_tokens=row.cache_creation_input_tokens,
            cache_read_input_tokens=row.cache_read_input_tokens,
        )
        for row in result.all()
    ]


async def enforce_quota(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Pre-call gate. No-op when disabled; raises 429 when at/over the limit."""
    limit = settings.ai_daily_token_quota
    if limit <= 0:
        return
    used = await get_today_total(db, user_id)
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "limit": limit,
                "used": used,
                "remaining": 0,
                "reset_at": _next_utc_midnight(_utc_today()).isoformat(),
            },
        )


async def record_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    usage: Usage,
    *,
    fallback_text: str = "",
) -> None:
    """Post-call accounting. No-op when disabled.

    Uses the provider-reported token counts. When both are zero (the provider
    omitted usage), fall back to a chars/4 estimate over fallback_text, booked
    as output tokens; precision is not required for budget enforcement. The
    cache counters pass straight through for observability and are never part of
    the fallback estimate or the quota total.
    """
    if settings.ai_daily_token_quota <= 0:
        return
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    if input_tokens == 0 and output_tokens == 0:
        output_tokens = max(1, len(fallback_text) // 4)
    await add_tokens(
        db,
        user_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # getattr-default so any duck-typed usage object missing the (optional)
        # cache fields still meters cleanly; a real Usage always supplies them.
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0),
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0),
    )
