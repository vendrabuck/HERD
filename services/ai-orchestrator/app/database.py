"""SQLAlchemy 2 async engine + declarative base for the ai-orchestrator.

Branch 3 (multi-turn chat) is the first feature in this service that needs
persistence; before that, the service ran fully stateless. The engine is
keyed to settings.database_url (Postgres in dev/prod, sqlite for tests) and
matches the pattern used by reservations, inventory, and the other DB-backed
services in the workspace.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
