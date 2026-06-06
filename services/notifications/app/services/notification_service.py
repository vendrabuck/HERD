import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification


async def list_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> tuple[list[Notification], int, int]:
    base = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
    total_q = select(func.count()).select_from(
        select(Notification).where(Notification.user_id == user_id).subquery()
    )
    unread_q = select(func.count()).select_from(
        select(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .subquery()
    )
    items_q = base.order_by(Notification.created_at.desc()).limit(limit).offset(offset)

    items = (await db.execute(items_q)).scalars().all()
    total = (await db.execute(total_q)).scalar_one()
    unread = (await db.execute(unread_q)).scalar_one()
    return list(items), int(total), int(unread)


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int(result.scalar_one())


async def mark_read(
    db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notification)
    return notification


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=now)
    )
    await db.commit()
    return int(result.rowcount or 0)


async def delete_one(db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
    result = await db.execute(
        delete(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    await db.commit()
    return (result.rowcount or 0) > 0


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    event_type: str,
    title: str,
    body: str,
    data: dict | None = None,
    dedupe_key: str | None = None,
) -> Notification | None:
    """Create a notification row, idempotent on (user_id, dedupe_key).

    When dedupe_key is set and a row for this (user_id, dedupe_key) already
    exists (a NATS redelivery of the same source message), the insert hits the
    partial-unique index, is rolled back, and None is returned so the caller can
    ack the duplicate as a no-op. With no dedupe_key the insert is unconstrained.
    """
    notification = Notification(
        user_id=user_id,
        event_type=event_type,
        title=title,
        body=body,
        data=data or {},
        dedupe_key=dedupe_key,
    )
    db.add(notification)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(notification)
    return notification
