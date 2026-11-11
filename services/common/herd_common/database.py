"""Shared SQLAlchemy 2 async engine/session/declarative-base factory.

Usage in a service's `app/database.py`:
    from herd_common.database import make_database

    from app.config import settings

    engine, AsyncSessionLocal, Base, get_db = make_database(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
    )

Each call returns its own `Base` (a fresh `DeclarativeBase` subclass), since every
service owns its own metadata and migration chain; do not share one `Base` across
services. `engine` and `AsyncSessionLocal` must be re-exported as real module-level
names in the calling service's `app/database.py`, not just returned locally: some
call sites (e.g. auth's LDAP sync `_SyncSlot`) read `database.engine` fresh at call
time via `from app import database`, and tests monkeypatch that module attribute, so
the module-level binding is load-bearing.

Connection pool (issue #1352): every service passes its `db_pool_size`,
`db_max_overflow`, and `db_pool_pre_ping` settings (HerdBaseSettings, env
`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_PRE_PING`), so one process holds at
most pool_size + max_overflow connections. The defaults below are those
settings' defaults: 5 + 5, so the eleven database services hold at most
11 x 10 = 110 connections, under the compose Postgres `max_connections=200`.
SQLite URLs (the services' unit tests) get only `pool_pre_ping`: SQLAlchemy
picks a pool for them that takes no size arguments (an in-memory database
uses StaticPool, which refuses `pool_size` and `max_overflow`).
"""

from collections.abc import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 5
DEFAULT_POOL_PRE_PING = True


def engine_options(
    database_url: str,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    pool_pre_ping: bool = DEFAULT_POOL_PRE_PING,
) -> dict[str, object]:
    """The keyword arguments make_database passes to create_async_engine."""
    options: dict[str, object] = {"echo": False, "pool_pre_ping": pool_pre_ping}
    if make_url(database_url).get_backend_name() != "sqlite":
        options["pool_size"] = pool_size
        options["max_overflow"] = max_overflow
    return options


def make_database(
    database_url: str,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    pool_pre_ping: bool = DEFAULT_POOL_PRE_PING,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], type[DeclarativeBase], object]:
    """Build an engine, sessionmaker, declarative base, and get_db dependency.

    Returns a 4-tuple of (engine, AsyncSessionLocal, Base, get_db).
    """
    engine = create_async_engine(
        database_url,
        **engine_options(
            database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=pool_pre_ping,
        ),
    )
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    class Base(DeclarativeBase):
        pass

    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSessionLocal() as session:
            yield session

    return engine, AsyncSessionLocal, Base, get_db
