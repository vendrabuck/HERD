import uuid

import pytest
from app.database import Base
from app.models.notification import Notification
from app.services.dispatchers import InAppDispatcher
from app.services.dispatchers.base import DispatchMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


def _session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._s = _SessionLocal()
            return self._s

        async def __aexit__(self, *args):
            await self._s.close()

    return _Ctx()


@pytest.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_in_app_dispatcher_persists_notification():
    dispatcher = InAppDispatcher()
    assert dispatcher.channel == "in_app"
    user_id = uuid.uuid4()
    message = DispatchMessage(
        user_id=user_id,
        event_type="reservation.created",
        title="Hi",
        body="body",
        data={"x": 1},
    )
    await dispatcher.send(_session_factory, message)

    async with _SessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.user_id == user_id))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Hi"
    assert rows[0].event_type == "reservation.created"
    assert rows[0].data == {"x": 1}
    assert rows[0].read_at is None


async def _count_for(user_id: uuid.UUID) -> int:
    async with _SessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.user_id == user_id))
        return len(result.scalars().all())


def _msg(user_id: uuid.UUID, dedupe_key: str | None = None) -> DispatchMessage:
    return DispatchMessage(
        user_id=user_id,
        event_type="reservation.created",
        title="Hi",
        body="body",
        dedupe_key=dedupe_key,
    )


@pytest.mark.asyncio
async def test_redelivery_is_idempotent():
    """Re-dispatching the same source message (same dedupe_key) for the same
    user creates exactly one row; the redelivery is a no-op."""
    dispatcher = InAppDispatcher()
    user_id = uuid.uuid4()
    await dispatcher.send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:42"))
    await dispatcher.send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:42"))
    assert await _count_for(user_id) == 1


@pytest.mark.asyncio
async def test_distinct_dedupe_keys_create_distinct_rows():
    """Different source messages (different sequence) each create a row."""
    dispatcher = InAppDispatcher()
    user_id = uuid.uuid4()
    await dispatcher.send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:42"))
    await dispatcher.send(_session_factory, _msg(user_id, "HERD_RESERVATIONS:43"))
    assert await _count_for(user_id) == 2


@pytest.mark.asyncio
async def test_same_key_different_users_each_get_a_row():
    """The dedupe is per (user, key): a fan-out of one event to two recipients
    creates one row each, and redelivery still collapses per user."""
    dispatcher = InAppDispatcher()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    await dispatcher.send(_session_factory, _msg(user_a, "HERD_RESERVATIONS:42"))
    await dispatcher.send(_session_factory, _msg(user_b, "HERD_RESERVATIONS:42"))
    # Redelivery of the same fan-out.
    await dispatcher.send(_session_factory, _msg(user_a, "HERD_RESERVATIONS:42"))
    await dispatcher.send(_session_factory, _msg(user_b, "HERD_RESERVATIONS:42"))
    assert await _count_for(user_a) == 1
    assert await _count_for(user_b) == 1


@pytest.mark.asyncio
async def test_null_dedupe_keys_are_unconstrained():
    """Rows with no dedupe_key (not event-sourced) are not deduplicated."""
    dispatcher = InAppDispatcher()
    user_id = uuid.uuid4()
    await dispatcher.send(_session_factory, _msg(user_id, None))
    await dispatcher.send(_session_factory, _msg(user_id, None))
    assert await _count_for(user_id) == 2
