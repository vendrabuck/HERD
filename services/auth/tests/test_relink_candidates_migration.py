"""Migration 0011 (issue #1399, owner decision 2026-11-18): the relink candidate
tables, federated_relink_candidates and federated_relink_capture_totals.

The same shape as test_oidc_sso_migration.py: the upgrade and downgrade run for
real through Alembic's Operations against an in-memory SQLite database whose
`users` table holds rows, and the migrated schema is compared with the one
create_all builds from the models, on SQLite by reflection and as Postgres DDL.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.database import Base
from app.models.federated_relink_candidate import (
    FederatedRelinkCandidate,
    FederatedRelinkCaptureTotal,
)
from app.models.user import User
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from tests.test_oidc_sso_migration import (
    _USER_ROWS,
    _ddl_shape,
    _engine_with_users,
    _normalize_ddl,
    _run,
    _script_directory,
    _shape,
    _users,
)

_AUTH_DIR = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _AUTH_DIR / "migrations" / "versions" / "0011_relink_candidates.py"
_spec = importlib.util.spec_from_file_location("relink_candidates_migration", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

_NEW_TABLES = ("federated_relink_candidates", "federated_relink_capture_totals")


def test_upgrade_creates_both_tables_and_downgrade_drops_them_keeping_users():
    engine = _engine_with_users()
    before = _users(engine)
    _run(engine, _migration.upgrade)
    assert set(_NEW_TABLES) <= set(sa.inspect(engine).get_table_names())
    _run(engine, _migration.downgrade)
    assert not set(_NEW_TABLES) & set(sa.inspect(engine).get_table_names())
    assert _users(engine) == before
    _run(engine, _migration.upgrade)
    assert set(_NEW_TABLES) <= set(sa.inspect(engine).get_table_names())


def test_upgrade_enforces_one_row_per_account_and_pair_and_cascades():
    engine = _engine_with_users()
    _run(engine, _migration.upgrade)
    uid = _USER_ROWS[0][0]
    insert = sa.text(
        "INSERT INTO federated_relink_candidates (id, user_id, issuer, subject, created_at,"
        " last_seen_at, expires_at, attempts) VALUES (:id, :uid, 'x', 's',"
        " '2026-11-18 00:00:00', '2026-11-18 00:00:00', '2026-11-19 00:00:00', 1)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "c1", "uid": uid})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"id": "c2", "uid": uid})
    shape = _shape(engine, "federated_relink_candidates")
    assert shape["uniques"] == [
        ("uq_federated_relink_candidates_user_pair", ("user_id", "issuer", "subject"))
    ]
    assert shape["fks"] == [
        ("fk_federated_relink_candidates_user_id_users", ("user_id",), "users", ("id",), "CASCADE")
    ]
    totals = _shape(engine, "federated_relink_capture_totals")
    assert totals["pk"] == ["user_id"]
    assert totals["fks"] == [
        (
            "fk_federated_relink_capture_totals_user_id_users",
            ("user_id",),
            "users",
            ("id",),
            "CASCADE",
        )
    ]


@pytest.mark.parametrize("table", _NEW_TABLES)
def test_create_all_and_the_migration_build_the_same_table(table):
    migrated = _engine_with_users()
    _run(migrated, _migration.upgrade)
    fresh = sa.create_engine("sqlite://")
    Base.metadata.create_all(
        fresh,
        tables=[
            User.__table__,
            FederatedRelinkCandidate.__table__,
            FederatedRelinkCaptureTotal.__table__,
        ],
    )
    assert _shape(migrated, table) == _shape(fresh, table)


def test_postgres_ddl_of_the_migration_equals_the_models():
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(ctx):
        _migration.upgrade()
    migration_ddl = [s for s in buf.getvalue().split(";") if "CREATE" in s]
    dialect = postgresql.dialect()
    model_ddl = []
    for model in (FederatedRelinkCandidate, FederatedRelinkCaptureTotal):
        model_ddl.append(str(CreateTable(model.__table__).compile(dialect=dialect)))
        for index in model.__table__.indexes:
            model_ddl.append(str(CreateIndex(index).compile(dialect=dialect)))
    assert _ddl_shape(migration_ddl) == _ddl_shape(model_ddl)
    assert "ON DELETE CASCADE" in " ".join(_normalize_ddl(migration_ddl))


def test_auth_migration_chain_is_linear_with_0011_at_head():
    script = _script_directory()
    assert script.get_heads() == ["0011"]
    assert script.get_revision("0011").down_revision == "0010"
