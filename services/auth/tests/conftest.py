"""Fixtures shared by the whole auth test suite.

This file now covers two seams, not one.

The original seam (issue #528): the LDAP stale-run reaper's session factory.
The reaper deliberately runs on its OWN session rather than the caller's, so
in production it opens one from app.database. Every LDAP sync test file,
however, builds a PRIVATE in-memory engine and hands run_sync a session from
it, which leaves the reaper looking at app.database's schema: no
ldap_sync_runs table, a swallowed exception, and a traceback in the captured
log that has nothing to do with the test. Worse, the reap those tests drive
is then a no-op against an empty database, so nothing they do can prove a
corpse was ever flipped. Pointing the reaper at the same engine the test
seeded fixes both: the noise goes away at its source, and the reap runs
against real rows.

The second seam (issue #511): the six test files that carried a private
in-memory-sqlite engine plus an identical create_all/drop_all setup_db
fixture and an identical dependency-override HTTP client now share ONE
engine (tests/_harness.py) and ONE create_all/drop_all fixture (setup_db,
below) plus ONE client-building fixture (make_client, below). The importable
pieces (the engine, the sessionmaker, override_get_db, the mock_user
builder) live in tests/_harness.py rather than here, because importing a
fixture out of conftest.py into a test module is a pytest anti-pattern;
fixtures stay here where pytest's own discovery finds them.
"""

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.user import User
from app.services import ldap_sync_service
from httpx import ASGITransport, AsyncClient

from tests._harness import engine, override_get_db


@pytest.fixture
def use_reap_session_factory(monkeypatch):
    """Point the stale-run reaper's own-session factory at a sessionmaker.

    A setter rather than a fixture that does the work itself, because the
    engine a test needs the reaper to see is not always known at fixture
    time: some files build it at module import, some in a fixture, some
    inside the test body. Each LDAP sync test file wires it once, usually
    from a three-line autouse fixture of its own.

    Deliberately NOT autouse here. Files that exercise the reaper's own
    session end to end (tests/test_ldap_sync_stale_run_reaper.py) must see
    the PRODUCTION default, app.database.AsyncSessionLocal, rather than a
    global override that would hide a broken default from every test at once.
    """

    def _use(session_factory) -> None:
        monkeypatch.setattr(ldap_sync_service, "_reap_session_factory", session_factory)

    return _use


@pytest.fixture(autouse=True)
async def setup_db():
    """Create and drop the shared in-memory schema around every test.

    Autouse and shared by the six harness-using test files (issue #511).
    Files that build their own private engine by design (the LDAP-sync
    service/live/stale-reaper/loop suites and the *_unit.py files) are
    untouched by this: they never import from tests._harness, so this
    fixture's create_all/drop_all runs against a table set that plays no
    part in their tests.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def make_client():
    """Return a factory for an ASGI-transport client wired to the test DB.

    `make_client()` builds an anonymous client (get_current_user left
    un-overridden, so requests behave as unauthenticated); `make_client(user)`
    additionally overrides get_current_user to return that user. Each of the
    six harness files wraps this in its own thin, file-named fixtures
    (admin_client, user_client, and so on) so test bodies stay unchanged.
    """

    def _make(user: User | None = None) -> AsyncClient:
        app.dependency_overrides[get_db] = override_get_db
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make
    app.dependency_overrides.clear()
