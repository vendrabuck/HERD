"""The stack's Postgres ran infra/postgres/init.sql to its last statement (issue #1325).

The official postgres image runs init files once, on a fresh volume, and a failing
statement stops the container after the earlier statements committed; the restart
then skips initialization. Before #1325 a non-default POSTGRES_USER or POSTGRES_DB
left exactly that state: every schema created, the database search path never set.
Every service still worked, so nothing else in CI would notice.

This reads, through the suite's `_psql` helper (`docker compose exec postgres psql`
as POSTGRES_USER on POSTGRES_DB, the same connection the init script used):

- the database-level `search_path` setting, which only init.sql's LAST statement
  writes, so its presence proves the file ran to the end; and
- the owner of every schema init.sql creates, which must be the connected user (the
  GRANTs #1325 removed were redundant only because of this).

The search path is checked against the prefix every version of init.sql has set
since the first one (2026-06-06), not the full current list: a stack whose volume an
older init.sql initialized carries a shorter list, and that is not a failure. CI's
integration job and the gates start Postgres on a fresh volume, where the full list
applies.
"""

from .conftest import _psql

SCHEMAS = [
    "auth",
    "inventory",
    "reservations",
    "acl",
    "user_profile",
    "cabling",
    "execution",
    "notifications",
    "ai_orchestrator",
    "integration",
    "secrets",
]
# The search path every init.sql version since the first has set, in this order.
SEARCH_PATH_PREFIX = "public, " + ", ".join(SCHEMAS[:10])


def _query(sql: str) -> list[str]:
    result = _psql(sql, tuples_only=True)
    assert result.returncode == 0, f"psql failed: {result.stderr.strip()}"
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_init_script_set_the_database_search_path():
    rows = _query(
        "SELECT array_to_string(s.setconfig, '|') FROM pg_db_role_setting s "
        "JOIN pg_database d ON d.oid = s.setdatabase "
        "WHERE d.datname = current_database() AND s.setrole = 0"
    )
    settings = [item for row in rows for item in row.split("|")]
    paths = [item.split("=", 1)[1] for item in settings if item.startswith("search_path=")]
    assert paths, (
        "the database has no search_path setting: infra/postgres/init.sql stopped before "
        f"its last statement on this volume (settings found: {settings})"
    )
    assert paths[0].startswith(SEARCH_PATH_PREFIX), paths[0]


def test_init_script_schemas_exist_and_belong_to_the_connected_user():
    names = ", ".join(f"'{name}'" for name in SCHEMAS)
    rows = _query(
        "SELECT nspname || ':' || (pg_get_userbyid(nspowner) = current_user)::text "
        f"FROM pg_namespace WHERE nspname IN ({names}) ORDER BY nspname"
    )
    found = dict(row.split(":", 1) for row in rows)
    assert sorted(found) == sorted(SCHEMAS), f"missing schemas: {set(SCHEMAS) - set(found)}"
    not_owned = sorted(name for name, owned in found.items() if owned != "true")
    assert not not_owned, f"schemas not owned by the connected user: {not_owned}"
