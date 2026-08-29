"""Fixtures shared by the reservations test files that use the private
in-memory-sqlite harness in tests/_harness.py (issue #628, mirrors auth's #511
split, PR #619).

setup_db is autouse and directory-scoped, so it runs for every test in this
directory, not only the ones that import from tests._harness. That is harmless by
construction: it creates and drops tables on the harness's own private engine, a
database instance no other test file in this directory touches (the files that
share app.database's engine instead, or build their own fixture-scoped engine, are
listed in _harness.py's module docstring). The importable pieces (the engine, the
sessionmaker, override_get_db, override_bearer) live in tests/_harness.py rather
than here, because importing a fixture out of conftest.py into a test module is a
pytest anti-pattern; fixtures stay here where pytest's own discovery finds them.
"""

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.routers.reservations import bearer_scheme
from httpx import ASGITransport, AsyncClient

from tests._harness import engine, override_bearer, override_get_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Create and drop the shared in-memory schema around every test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def make_client():
    """Return a factory for a dependency-override ASGI client wired to the test DB.

    `make_client()` builds an anonymous client (get_current_user_payload left
    un-overridden); `make_client(payload)` additionally overrides
    get_current_user_payload to return that JWT-claims dict (sub/username/role).
    Most migrated files keep their own small, differently-shaped client helper
    (a fixed user, a sub/role-parameterized helper, admin-vs-non-admin fixture
    pairs) rather than forcing every call site onto this one signature; this
    factory is here for files that want it directly.
    """

    def _make(payload: dict | None = None) -> AsyncClient:
        app.dependency_overrides[get_db] = override_get_db
        if payload is not None:
            app.dependency_overrides[get_current_user_payload] = lambda: payload
        app.dependency_overrides[bearer_scheme] = override_bearer
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make
    app.dependency_overrides.clear()
