"""GET /api/ai/usage: admin-only per-user daily AI token usage, for reporting.

Returns one entry per (user_id, usage_date) over an inclusive UTC date range,
carrying both the quota-bearing input/output counts and the observability-only
prompt-cache counters. This backs the AI usage panel on the admin reporting
page (issue #64). It is a read over the ai_orchestrator schema only and, like
the other admin reporting endpoints, requires the admin or superadmin role; it
is not gated on ai_is_configured() since historical usage is meaningful
regardless of current provider configuration.

Both query params are optional UTC dates (YYYY-MM-DD). When omitted, end
defaults to today and start to 30 days before end. The range is inclusive on
both ends; start must not be after end, and the span is capped to bound the
scan.
"""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from herd_common.auth import make_auth_dependencies
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import usage_repo

_get_current_user, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/usage", tags=["usage"])

# Bound the queryable window so a single request can't scan an unbounded range.
_MAX_RANGE_DAYS = 366


def _utc_today() -> date:
    return datetime.now(UTC).date()


@router.get("")
async def get_usage(
    start: date | None = Query(default=None, description="Inclusive UTC start date (YYYY-MM-DD)."),
    end: date | None = Query(default=None, description="Inclusive UTC end date (YYYY-MM-DD)."),
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    end = end or _utc_today()
    start = start or (end - timedelta(days=30))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must not be after end",
        )
    if (end - start).days > _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"date range too large; max {_MAX_RANGE_DAYS} days",
        )
    rows = await usage_repo.query_usage(db, start=start, end=end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "usage": [
            {
                "user_id": str(row.user_id),
                "date": row.usage_date.isoformat(),
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cache_creation_input_tokens": row.cache_creation_input_tokens,
                "cache_read_input_tokens": row.cache_read_input_tokens,
            }
            for row in rows
        ],
    }
