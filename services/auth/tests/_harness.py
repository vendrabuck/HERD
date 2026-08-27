"""Shared, importable pieces for the in-memory-DB test files (issue #511).

Six test files (test_api_tokens.py, test_internal.py, test_groups.py,
test_ldap_sync.py, test_auth.py, test_routers_direct.py) each carried a
byte-identical block: an in-memory sqlite engine, a TestSessionLocal
sessionmaker, an override_get_db dependency, and a copy of a mock-user
builder that had drifted into five slightly different shapes across the six
files. This module is the single source for the importable pieces
(TEST_DATABASE_URL, engine, TestSessionLocal, override_get_db, mock_user);
the setup_db fixture and the make_client fixture live in conftest.py instead.

That split is deliberate, not arbitrary: importing a fixture from conftest.py
into a test module is a pytest anti-pattern (conftest fixtures are meant to
be discovered by pytest's fixture machinery via the directory tree, not
imported as regular symbols), so anything that needs `import` rather than
autouse/dependency-injection has to live in a plain module. tests/ is a
package (tests/__init__.py exists), so `from tests._harness import ...`
resolves under `uv run pytest` from the service directory; this was verified
directly rather than assumed.
"""

import uuid

from app.models.user import Role, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def mock_user(
    role: Role = Role.USER,
    *,
    user_id: uuid.UUID | None = None,
    username: str = "mock",
    email: str | None = None,
) -> User:
    """Build an unsaved User for dependency-override auth in HTTP tests.

    Mirrors the five drifted per-file helpers this replaces (_make_mock_user
    in test_groups/test_auth/test_ldap_sync, _mock_user in
    test_api_tokens/test_routers_direct): a fixed id when the caller cares
    about it (test_groups, test_auth), a generated one otherwise, and an
    email that defaults from the username unless the caller needs a specific
    one (test_routers_direct's "mock@test.com").
    """
    return User(
        id=user_id or uuid.uuid4(),
        email=email or f"{username}@test.com",
        username=username,
        hashed_password="fake",
        is_active=True,
        role=role,
    )
