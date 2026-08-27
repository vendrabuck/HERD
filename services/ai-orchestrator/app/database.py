"""SQLAlchemy 2 async engine + declarative base for the ai-orchestrator.

Branch 3 (multi-turn chat) is the first feature in this service that needs
persistence; before that, the service ran fully stateless. The engine is
keyed to settings.database_url (Postgres in dev/prod, sqlite for tests) and
matches the pattern used by reservations, inventory, and the other DB-backed
services in the workspace.
"""

from herd_common.database import make_database
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 (re-exported for callers/tests)

from app.config import settings

engine, AsyncSessionLocal, Base, get_db = make_database(settings.database_url)
