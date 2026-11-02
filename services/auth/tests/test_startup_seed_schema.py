"""The startup seed boots on the previous release's schema (issue #1261).

Auth's lifespan runs create_all_and_stamp, which is a no-op on a stamped
schema (issue #419), and then the two seeds. Both run before `make migrate`
can, so between an image recreate and the migrate the users table still has
the PREVIOUS release's columns. A seed read that selects the whole User model
names every mapped column, and one column the stamped schema lacks (revision
0009 added full_name, contact_email, and phone) made the SELECT fail, the
exception escape lifespan, and auth never become healthy.

Two kinds of proof, both at the database rather than at a Python name:

- every statement the seeds send is recorded through a before_cursor_execute
  listener on the shared harness engine (the shape
  tests/test_user_profile_routes.py uses), and each SELECT must select the id
  column only, so no column a later release adds can ever be named there;
- the seeds run against a users table built in the previous shape (the
  harness schema with the three profile columns dropped, rows inserted by raw
  SQL) and must return without raising and without writing.
"""

import contextlib
import re
import sqlite3
import uuid
from unittest.mock import patch

import pytest
from app.models.user import Role
from app.services.auth_service import create_user, superadmin_exists
from sqlalchemy import event, text

from tests._harness import TestSessionLocal, engine

PROFILE_COLUMNS = ("full_name", "contact_email", "phone")

_SELECT_LIST = re.compile(r"^\s*SELECT\s+(.*?)\s+FROM\s", re.IGNORECASE | re.DOTALL)

needs_drop_column = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35, 0),
    reason=f"ALTER TABLE ... DROP COLUMN needs SQLite 3.35, this is {sqlite3.sqlite_version}",
)


@contextlib.contextmanager
def _statements():
    """Record every SQL statement the harness engine sends while open."""
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)


def _select_lists(statements: list[str]) -> list[str]:
    """The select list of every SELECT among the statements, whitespace collapsed."""
    out = []
    for statement in statements:
        match = _SELECT_LIST.match(statement)
        if match:
            out.append(" ".join(match.group(1).split()))
    return out


@contextlib.contextmanager
def _seed_env(email: str = "sa@test.com", username: str = "superadmin"):
    """Point the seeds at the harness database with all three SUPERADMIN_* set."""
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = email
        mock_settings.superadmin_username = username
        mock_settings.superadmin_password = "strongpass1"
        yield


async def _drop_profile_columns() -> None:
    """Turn the harness users table into its shape before revision 0009."""
    async with engine.begin() as conn:
        for column in PROFILE_COLUMNS:
            await conn.execute(text(f"ALTER TABLE users DROP COLUMN {column}"))
        names = {row[1] for row in (await conn.execute(text("PRAGMA table_info(users)")))}
    assert names.isdisjoint(PROFILE_COLUMNS), names


async def _insert_old_shape_user(email: str, username: str, role: Role) -> None:
    """Insert a user by raw SQL, naming only the columns the old shape has.

    Uuid(as_uuid=True) is stored as 32 hex characters on SQLite and an Enum
    column stores the member NAME, so this row reads back like an ORM one.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, username, hashed_password, auth_source,"
                " is_active, deactivated_by_sync, role)"
                " VALUES (:id, :email, :username, 'x', 'local', 1, 0, :role)"
            ),
            {"id": uuid.uuid4().hex, "email": email, "username": username, "role": role.name},
        )


async def _user_rows() -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT email, role FROM users ORDER BY email"))
        return [tuple(row) for row in result]


async def _group_names() -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT name FROM user_groups ORDER BY name"))
        return [row[0] for row in result]


# --- the reads the seeds send ---


@pytest.mark.asyncio
async def test_superadmin_existence_check_selects_the_id_only():
    async with TestSessionLocal() as db:
        await create_user(db, "sa@test.com", "superadmin", "strongpass1", Role.SUPERADMIN)

    async with TestSessionLocal() as db:
        with _statements() as seen:
            assert await superadmin_exists(db) is True

    assert _select_lists(seen) == ["users.id"], seen
    for statement in seen:
        for column in PROFILE_COLUMNS:
            assert column not in statement, statement


@pytest.mark.asyncio
async def test_startup_seeds_on_an_existing_stack_select_ids_only():
    """The path every upgraded stack takes: a superadmin and the default group
    already exist, so each seed sends one existence check and writes nothing."""
    from app.main import _seed_not_grouped, _seed_superadmin

    async with TestSessionLocal() as db:
        await create_user(db, "sa@test.com", "superadmin", "strongpass1", Role.SUPERADMIN)

    with _seed_env():
        await _seed_not_grouped()
        with _statements() as seen:
            await _seed_superadmin()
            await _seed_not_grouped()

    assert _select_lists(seen) == ["users.id", "user_groups.id"], seen
    assert not [s for s in seen if not _SELECT_LIST.match(s)], seen


@pytest.mark.asyncio
async def test_seed_email_check_selects_the_id_only():
    """No superadmin, and the configured email belongs to a regular user: the
    seed's second existence check also selects the id only, then stops."""
    from app.main import _seed_superadmin

    async with TestSessionLocal() as db:
        await create_user(db, "taken@test.com", "takenuser", "password123", Role.USER)

    with _seed_env(email="taken@test.com"):
        with _statements() as seen:
            await _seed_superadmin()

    assert _select_lists(seen) == ["users.id", "users.id"], seen
    assert await _user_rows() == [("taken@test.com", "USER")]


# --- the previous release's schema ---


@needs_drop_column
@pytest.mark.asyncio
async def test_seed_completes_on_a_users_table_without_the_profile_columns():
    from app.main import _seed_not_grouped, _seed_superadmin

    await _drop_profile_columns()
    await _insert_old_shape_user("sa@test.com", "superadmin", Role.SUPERADMIN)

    with _seed_env(email="new@test.com", username="newsa"):
        await _seed_superadmin()
        await _seed_not_grouped()
        await _seed_not_grouped()

    assert await _user_rows() == [("sa@test.com", "SUPERADMIN")]
    assert await _group_names() == ["Not Grouped"]


@needs_drop_column
@pytest.mark.asyncio
async def test_seed_email_check_completes_on_a_users_table_without_the_profile_columns():
    """The seed's other read on the old shape: no superadmin, the email taken by
    a regular user, so the seed warns and returns without writing."""
    from app.main import _seed_superadmin

    await _drop_profile_columns()
    await _insert_old_shape_user("taken@test.com", "takenuser", Role.USER)

    with _seed_env(email="taken@test.com"):
        await _seed_superadmin()

    assert await _user_rows() == [("taken@test.com", "USER")]
