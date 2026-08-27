"""Shared count-then-page query helper (issue #597).

Six list endpoints across auth, acl, and notifications each hand-rolled the
same two-query pattern: a `select(func.count()).select_from(...)` for the
total, then an `order_by(...).offset(...).limit(...)` for the page, returned
as `(items, total)`. This module extracts that pattern once.

Ordering stays entirely with the caller. The statement passed in must already
carry its filters and its `ORDER BY` (including any id tiebreaker, as
`app/routers/ldap_sync.py`'s `list_mappings` and `list_sync_runs` use to keep
pages deterministic when timestamps tie): this helper never imposes or
assumes an order of its own.
"""

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def paginate(
    db: AsyncSession,
    stmt: Select,
    *,
    skip: int,
    limit: int,
) -> tuple[list[Any], int]:
    """Run `stmt` as a count-then-page query and return `(items, total)`.

    The total is computed from `select(func.count()).select_from(...)` over
    the statement with its `ORDER BY` dropped (`stmt.order_by(None)`) before
    wrapping it in a subquery. Dropping the order for the count only is a
    pure optimization, not a semantic change: an `ORDER BY` cannot affect how
    many rows a query returns, and some backends otherwise carry the sort
    into the count subquery for no benefit. The page itself is fetched from
    `stmt.offset(skip).limit(limit)`, unmodified, so the caller's ordering
    (and any tiebreaker) governs which rows land on which page.
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    page_stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(page_stmt)
    items = list(result.scalars().all())

    return items, total
