import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import UserPreferences


async def get_or_create(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences:
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    prefs = result.scalar_one_or_none()
    if prefs is not None:
        return prefs
    prefs = UserPreferences(
        user_id=user_id,
        saved_filters={},
        page_sizes={},
        extras={},
    )
    db.add(prefs)
    await db.commit()
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
