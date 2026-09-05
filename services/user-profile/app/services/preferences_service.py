import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import UserPreferences
from app.schemas.preferences import _validate_blob, _validate_page_sizes


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
    """Shallow-merge each supplied dict into the stored one.

    The request schema caps only the INCOMING dict; the caps must also hold on
    the MERGED result, or repeated patches grow one JSONB row without bound
    (issue #714). Every merge is validated before any attribute is assigned,
    so a rejected PATCH leaves the row exactly as it was. The router does not
    translate a service ValueError, so the 422 is raised here explicitly.
    """
    prefs = await get_or_create(db, user_id)
    merged_filters = _merge(prefs.saved_filters, saved_filters)
    merged_sizes = _merge(prefs.page_sizes, page_sizes)
    merged_extras = _merge(prefs.extras, extras)
    try:
        _validate_blob(merged_filters, "saved_filters")
        _validate_page_sizes(merged_sizes)
        _validate_blob(merged_extras, "extras")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"merged {exc}",
        ) from exc
    if merged_filters is not None:
        prefs.saved_filters = merged_filters
    if merged_sizes is not None:
        prefs.page_sizes = merged_sizes
    if merged_extras is not None:
        prefs.extras = merged_extras
    await db.commit()
    await db.refresh(prefs)
    return prefs


def _merge(current: dict, incoming: dict | None) -> dict | None:
    """Return current updated by incoming, or None when incoming is None (no change)."""
    if incoming is None:
        return None
    merged = dict(current)
    merged.update(incoming)
    return merged


async def reset(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences:
    prefs = await get_or_create(db, user_id)
    prefs.saved_filters = {}
    prefs.page_sizes = {}
    prefs.extras = {}
    await db.commit()
    await db.refresh(prefs)
    return prefs
