import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import UserPreferences


async def _select_prefs(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences | None:
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences:
    """Return the user's preferences row, creating an empty one on first access.

    The create is idempotent under concurrency. user_id is the primary key, so
    two requests that both observe no row will each try to insert; the loser's
    commit raises IntegrityError (Postgres unique violation). We roll that back
    and re-read the row the winner committed, converging on the single committed
    row instead of surfacing an unhandled 500. This is dialect-agnostic and works
    on both Postgres (asyncpg) in prod and SQLite (aiosqlite) in tests.
    """
    prefs = await _select_prefs(db, user_id)
    if prefs is not None:
        return prefs
    prefs = UserPreferences(
        user_id=user_id,
        saved_filters={},
        page_sizes={},
        extras={},
    )
    db.add(prefs)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prefs = await _select_prefs(db, user_id)
        if prefs is None:
            # The conflicting writer's row should be visible after rollback; if
            # not, the IntegrityError was not the expected PK race, so re-raise.
            raise
        return prefs
    await db.refresh(prefs)
    return prefs


async def replace(
    db: AsyncSession,
    user_id: uuid.UUID,
    saved_filters: dict,
    page_sizes: dict,
    extras: dict,
) -> UserPreferences:
    prefs = await get_or_create(db, user_id)
    prefs.saved_filters = saved_filters
    prefs.page_sizes = page_sizes
    prefs.extras = extras
    await db.commit()
    await db.refresh(prefs)
    return prefs


async def patch(
    db: AsyncSession,
    user_id: uuid.UUID,
    saved_filters: dict | None,
    page_sizes: dict | None,
    extras: dict | None,
) -> UserPreferences:
    prefs = await get_or_create(db, user_id)
    if saved_filters is not None:
        merged = dict(prefs.saved_filters)
        merged.update(saved_filters)
        prefs.saved_filters = merged
    if page_sizes is not None:
        merged = dict(prefs.page_sizes)
        merged.update(page_sizes)
        prefs.page_sizes = merged
    if extras is not None:
        merged = dict(prefs.extras)
        merged.update(extras)
        prefs.extras = merged
    await db.commit()
    await db.refresh(prefs)
    return prefs


async def reset(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences:
    prefs = await get_or_create(db, user_id)
    prefs.saved_filters = {}
    prefs.page_sizes = {}
    prefs.extras = {}
    await db.commit()
    await db.refresh(prefs)
    return prefs
