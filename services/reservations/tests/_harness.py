"""Shared, importable pieces for the reservations test files that build a private
in-memory-sqlite database plus a dependency-override HTTP client (issue #628).

Six test files (test_fork_endpoints.py, test_fork_version_endpoints.py,
test_wiring_proxy_endpoints.py, test_rbac_denial.py, test_coverage_gaps.py,
test_reservations.py) each carried a byte-identical block: a module-level
create_async_engine(TEST_DATABASE_URL, echo=False), an async_sessionmaker(...) as
TestSessionLocal, an override_get_db that yields from TestSessionLocal, and an
override_bearer that stands in for the bearer-scheme security dependency. This
module is the single source for those importable pieces (TEST_DATABASE_URL, engine,
TestSessionLocal, override_get_db, override_bearer); the setup_db fixture lives in
conftest.py instead, for the same reason auth's #511 split them: importing a
fixture out of conftest.py into a test module is a pytest anti-pattern (conftest
fixtures are meant to be discovered by pytest's own directory-tree machinery, not
imported as regular symbols).

Each file's own get_current_user_payload override and its client-building helper
(_client_as, client/other_client, user_client, and so on) are deliberately NOT
folded in here: they differ in shape from file to file (a fixed anonymous user, a
sub/role-parameterized helper, admin-vs-non-admin fixture pairs), and every one of
those variants already reduces to a handful of lines once it no longer has to also
declare the engine/session/bearer block. Each file keeps its own small version
rather than forcing every call site onto one shared signature.

Nine test files in this directory open their session against app.database's own
engine instead of a private one (test_fork_archive_reconcile.py,
test_fork_backstop_giveup.py, test_pending_fork_prune.py,
test_wiring_changed_staging.py, test_expiration.py, test_expiry_reminder.py,
test_dynamic_requests.py, and test_reservation_service_unit.py, which patches
app.tasks.expiration.AsyncSessionLocal directly): they exercise app.tasks.expiration
background functions whose AsyncSessionLocal is bound to app.database's engine at
import time, so a private harness engine that app.database never sees would leave
those functions reading an empty database. They are not migrated here for that
reason, mirroring auth's LDAP-sync exception in #511. test_reporting_edges.py,
test_reporting_service.py, and test_fleet_report.py's non-route tests use a
function-scoped db_session fixture with no get_db override and no HTTP client at
all, so they never carried this scaffold in the first place.
"""

from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_bearer() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")
