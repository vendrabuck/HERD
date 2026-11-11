"""Migration 0009 (issue #1252): profile columns, the full_name backfill, and
the auth migration chain.

The upgrade and downgrade functions are run for real through Alembic's own
Operations against a sync in-memory SQLite database holding rows that
predate the migration, so the backfill is proven on existing data rather
than on a fresh create_all schema (where the NOT NULL column never needed
one). The migration file is loaded by path; it has no side effects at import.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

_AUTH_DIR = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _AUTH_DIR / "migrations" / "versions" / "0009_user_profile.py"

_spec = importlib.util.spec_from_file_location("user_profile_migration", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

_PROFILE_COLUMNS = {"full_name", "contact_email", "phone"}


def _pre_0009_engine() -> sa.Engine:
    """A users table shaped as revision 0008 left it (the columns this test
    needs), holding two rows."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE users ("
                " id VARCHAR(36) PRIMARY KEY,"
                " email VARCHAR(255) NOT NULL UNIQUE,"
                " username VARCHAR(100) NOT NULL UNIQUE,"
                " hashed_password TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO users (id, email, username) VALUES"
                " ('1', 'ada@example.com', 'ada'), ('2', 'grace@example.com', 'grace')"
            )
        )
    return engine


def _run(engine: sa.Engine, step) -> None:
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


def _columns(engine: sa.Engine) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("users")}


def test_upgrade_backfills_full_name_from_username_on_existing_rows():
    engine = _pre_0009_engine()
    _run(engine, _migration.upgrade)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT username, full_name, contact_email, phone FROM users ORDER BY id")
        ).all()
    assert [tuple(r) for r in rows] == [("ada", "ada", None, None), ("grace", "grace", None, None)]


def test_upgrade_leaves_full_name_not_null_and_the_other_two_nullable():
    engine = _pre_0009_engine()
    _run(engine, _migration.upgrade)
    columns = _columns(engine)
    assert columns["full_name"]["nullable"] is False
    assert columns["contact_email"]["nullable"] is True
    assert columns["phone"]["nullable"] is True
    assert columns["full_name"]["type"].length == 200
    assert columns["contact_email"]["type"].length == 255
    assert columns["phone"]["type"].length == 32
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, email, username, full_name)"
                    " VALUES ('3', 'x@example.com', 'x', NULL)"
                )
            )


def test_downgrade_drops_the_three_columns_and_keeps_the_rows():
    engine = _pre_0009_engine()
    _run(engine, _migration.upgrade)
    _run(engine, _migration.downgrade)
    assert _PROFILE_COLUMNS.isdisjoint(_columns(engine))
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 2


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_AUTH_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_AUTH_DIR / "migrations"))
    # Same reason as cabling's chain test: an unset path_separator is a
    # DeprecationWarning, which filterwarnings = ["error"] turns into a failure.
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg)


def test_0009_follows_0008_in_the_auth_migration_chain():
    """The chain's linearity and its head are pinned by the newest revision's
    test (test_oidc_sso_migration.py since 0010); this one keeps 0009's place."""
    script = _script_directory()
    assert script.get_revision("0009").down_revision == "0008"
    assert "0009" in {r.revision for r in script.walk_revisions()}


def test_fresh_schema_declares_the_same_columns_as_the_migration():
    """create_all_and_stamp builds a fresh schema from the model, so the model
    must declare what 0009 leaves behind."""
    from app.models.user import User

    table = User.__table__
    assert table.c.full_name.nullable is False
    assert table.c.full_name.type.length == 200
    assert table.c.contact_email.nullable is True
    assert table.c.contact_email.type.length == 255
    assert table.c.phone.nullable is True
    assert table.c.phone.type.length == 32
