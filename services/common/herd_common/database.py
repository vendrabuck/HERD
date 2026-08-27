"""Shared SQLAlchemy 2 async engine/session/declarative-base factory.

Usage in a service's `app/database.py`:
    from herd_common.database import make_database

    from app.config import settings

    engine, AsyncSessionLocal, Base, get_db = make_database(settings.database_url)

Each call returns its own `Base` (a fresh `DeclarativeBase` subclass), since every
service owns its own metadata and migration chain; do not share one `Base` across
services. `engine` and `AsyncSessionLocal` must be re-exported as real module-level
names in the calling service's `app/database.py`, not just returned locally: some
call sites (e.g. auth's LDAP sync `_SyncSlot`) read `database.engine` fresh at call
time via `from app import database`, and tests monkeypatch that module attribute, so
the module-level binding is load-bearing.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


def make_database(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], type[DeclarativeBase], object]:
    """Build an engine, sessionmaker, declarative base, and get_db dependency.

    Returns a 4-tuple of (engine, AsyncSessionLocal, Base, get_db).
    """
    engine = create_async_engine(database_url, echo=False)
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    class Base(DeclarativeBase):
        pass

    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSessionLocal() as session:
            yield session

    return engine, AsyncSessionLocal, Base, get_db
