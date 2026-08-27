from herd_common.database import make_database
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 (re-exported for callers/tests)

from app.config import settings

engine, AsyncSessionLocal, Base, get_db = make_database(settings.database_url)
