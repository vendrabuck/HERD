"""Migration 0010 (ADR 0016 phase 2, issue #37): oidc_login_flows and
federated_identities.

The upgrade and downgrade run for real through Alembic's own Operations against
a sync in-memory SQLite database whose `users` table already holds rows (the
shape of test_user_profile_migration.py). The two tables exist only from 0010
on, so 0010 is the whole chain for them; the earlier revisions cannot replay on
SQLite (0004 alters a column in place), so `users` is built from the model,
which test_user_profile_migration.py pins to what 0009 leaves.

Parity: a fresh schema is built by create_all (create_all_and_stamp at startup)
and an existing one by this migration, so the two must agree. That is checked
twice: by reflecting both SQLite schemas (columns, types, nullability, server
defaults, primary keys, foreign keys with ON DELETE, unique constraints, check
constraints, indexes, every constraint name), and by comparing the Postgres DDL
each side emits (types such as UUID and TIMESTAMP WITH TIME ZONE never show on
SQLite).
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.database import Base
from app.models.federated_identity import FederatedIdentity
from app.models.oidc_login_flow import OidcLoginFlow
from app.models.user import User
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

_AUTH_DIR = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _AUTH_DIR / "migrations" / "versions" / "0010_oidc_sso.py"

_spec = importlib.util.spec_from_file_location("oidc_sso_migration", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

_NEW_TABLES = ("oidc_login_flows", "federated_identities")
_USER_ROWS = [
    ("00000000000000000000000000000001", "ada@example.com", "ada", "local"),
    ("00000000000000000000000000000002", "grace@example.com", "grace", "ldap"),
]


def _engine_with_users() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    User.__table__.create(engine)
    with engine.begin() as conn:
        for uid, email, username, source in _USER_ROWS:
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, email, username, full_name, auth_source, role,"
                    " is_active, deactivated_by_sync)"
                    " VALUES (:id, :email, :username, :username, :source, 'USER', 1, 0)"
                ),
                {"id": uid, "email": email, "username": username, "source": source},
            )
    return engine


def _run(engine: sa.Engine, step) -> None:
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


def _users(engine: sa.Engine) -> list[tuple]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                sa.text(
                    "SELECT id, email, username, full_name, auth_source, role, is_active"
                    " FROM users ORDER BY id"
                )
            )
        ]


def _shape(engine: sa.Engine, table: str) -> dict:
    insp = sa.inspect(engine)
    return {
        "columns": sorted(
            (c["name"], str(c["type"]), c["nullable"], c["default"])
            for c in insp.get_columns(table)
        ),
        "pk": insp.get_pk_constraint(table)["constrained_columns"],
        "fks": sorted(
            (
                fk["name"],
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                fk["options"].get("ondelete"),
            )
            for fk in insp.get_foreign_keys(table)
        ),
        "uniques": sorted(
            (u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(table)
        ),
        "checks": sorted(
            (c["name"], re.sub(r"\s+", " ", c["sqltext"]).strip())
            for c in insp.get_check_constraints(table)
        ),
        "indexes": sorted(
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in insp.get_indexes(table)
        ),
    }


def test_upgrade_creates_both_tables_and_leaves_users_rows_untouched():
    engine = _engine_with_users()
    before = _users(engine)
    _run(engine, _migration.upgrade)
    assert set(_NEW_TABLES) <= set(sa.inspect(engine).get_table_names())
    assert _users(engine) == before


def test_upgrade_declares_the_constraints_the_adr_names():
    engine = _engine_with_users()
    _run(engine, _migration.upgrade)
    flows = _shape(engine, "oidc_login_flows")
    idents = _shape(engine, "federated_identities")

    assert flows["pk"] == ["id"] and idents["pk"] == ["id"]
    assert ("uq_oidc_login_flows_state_hash", ("state_hash",)) in flows["uniques"]
    assert ("uq_oidc_login_flows_handoff_hash", ("handoff_hash",)) in flows["uniques"]
    assert flows["fks"] == [
        ("fk_oidc_login_flows_user_id_users", ("user_id",), "users", ("id",), "CASCADE")
    ]
    assert flows["indexes"] == [("ix_oidc_login_flows_expires_at", ("expires_at",), False)]
    assert [name for name, _ in flows["checks"]] == ["ck_oidc_login_flows_status"]
    nullable = {name: null for name, _, null, _ in flows["columns"]}
    assert nullable == {
        "id": False,
        "state_hash": False,
        "binding_hash": False,
        "nonce": False,
        "code_verifier": True,
        "next_path": False,
        "status": False,
        "user_id": True,
        "handoff_hash": True,
        "created_at": False,
        "expires_at": False,
        "handoff_expires_at": True,
    }

    assert idents["uniques"] == [
        ("uq_federated_identities_issuer_subject", ("issuer", "subject")),
        ("uq_federated_identities_user_id", ("user_id",)),
    ]
    assert idents["fks"] == [
        ("fk_federated_identities_user_id_users", ("user_id",), "users", ("id",), "CASCADE")
    ]
    types = {name: typ for name, typ, _, _ in idents["columns"]}
    assert types["subject"] == "VARCHAR(255)"
    assert types["issuer"] == "TEXT"


def test_upgrade_enforces_the_constraints_on_rows():
    engine = _engine_with_users()
    _run(engine, _migration.upgrade)
    uid = _USER_ROWS[0][0]
    insert_flow = sa.text(
        "INSERT INTO oidc_login_flows (id, state_hash, binding_hash, nonce, next_path, status,"
        " expires_at) VALUES (:id, :state, 'b', 'n', '/', :status, '2026-11-11 12:00:00')"
    )
    with engine.begin() as conn:
        conn.execute(insert_flow, {"id": "f1", "state": "s1", "status": "pending"})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert_flow, {"id": "f2", "state": "s1", "status": "pending"})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert_flow, {"id": "f3", "state": "s3", "status": "bogus"})

    insert_ident = sa.text(
        "INSERT INTO federated_identities (id, user_id, issuer, subject)"
        " VALUES (:id, :user_id, :issuer, :subject)"
    )
    with engine.begin() as conn:
        conn.execute(insert_ident, {"id": "i1", "user_id": uid, "issuer": "x", "subject": "s"})
    for row in (
        {"id": "i2", "user_id": _USER_ROWS[1][0], "issuer": "x", "subject": "s"},
        {"id": "i3", "user_id": uid, "issuer": "y", "subject": "t"},
    ):
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(insert_ident, row)


def test_downgrade_drops_both_tables_and_keeps_users_rows():
    engine = _engine_with_users()
    before = _users(engine)
    _run(engine, _migration.upgrade)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO federated_identities (id, user_id, issuer, subject)"
                " VALUES ('i1', :uid, 'x', 's')"
            ),
            {"uid": _USER_ROWS[0][0]},
        )
    _run(engine, _migration.downgrade)
    assert not set(_NEW_TABLES) & set(sa.inspect(engine).get_table_names())
    assert _users(engine) == before
    # And the upgrade applies again cleanly after a downgrade.
    _run(engine, _migration.upgrade)
    assert set(_NEW_TABLES) <= set(sa.inspect(engine).get_table_names())


@pytest.mark.parametrize("table", _NEW_TABLES)
def test_create_all_and_the_migration_build_the_same_table(table):
    migrated = _engine_with_users()
    _run(migrated, _migration.upgrade)
    fresh = sa.create_engine("sqlite://")
    Base.metadata.create_all(
        fresh, tables=[User.__table__, OidcLoginFlow.__table__, FederatedIdentity.__table__]
    )
    assert _shape(migrated, table) == _shape(fresh, table)


def _normalize_ddl(statements: list[str]) -> list[str]:
    out = []
    for stmt in statements:
        body = re.sub(r"\s+", " ", stmt).strip().rstrip(";").strip()
        if body:
            out.append(body)
    return out


def _split_create_table(stmt: str) -> tuple[str, frozenset[str]]:
    """CREATE TABLE text as (head, set of column and constraint clauses), so the
    order in which each side lists its clauses does not matter."""
    head, _, rest = stmt.partition("(")
    inner = rest.rsplit(")", 1)[0]
    clauses, depth, current = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            clauses.append(current.strip())
            current = ""
        else:
            current += ch
    clauses.append(current.strip())
    return head.strip(), frozenset(clauses)


def _ddl_shape(statements: list[str]) -> set:
    shape = set()
    for stmt in _normalize_ddl(statements):
        if stmt.startswith("CREATE TABLE"):
            shape.add(_split_create_table(stmt))
        else:
            shape.add(stmt)
    return shape


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
    for model in (OidcLoginFlow, FederatedIdentity):
        model_ddl.append(str(CreateTable(model.__table__).compile(dialect=dialect)))
        for index in model.__table__.indexes:
            model_ddl.append(str(CreateIndex(index).compile(dialect=dialect)))

    migration_shape = _ddl_shape(migration_ddl)
    assert migration_shape == _ddl_shape(model_ddl)
    joined = " ".join(_normalize_ddl(migration_ddl))
    assert "UUID" in joined and "TIMESTAMP WITH TIME ZONE" in joined
    assert "ON DELETE CASCADE" in joined


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_AUTH_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_AUTH_DIR / "migrations"))
    # An unset path_separator is a DeprecationWarning, which
    # filterwarnings = ["error"] turns into a failure.
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg)


def test_auth_migration_chain_is_linear_with_0010_at_head():
    script = _script_directory()
    assert script.get_heads() == ["0010"]
    revisions = list(script.walk_revisions())
    on_disk = {
        p.stem.split("_", 1)[0] for p in (_AUTH_DIR / "migrations" / "versions").glob("*.py")
    }
    assert {r.revision for r in revisions} == on_disk
    assert [r.revision for r in revisions if r.down_revision is None] == ["0001"]
    down_revisions = [r.down_revision for r in revisions if r.down_revision is not None]
    assert len(down_revisions) == len(set(down_revisions))
    assert script.get_revision("0010").down_revision == "0009"
