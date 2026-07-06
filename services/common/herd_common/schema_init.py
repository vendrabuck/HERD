"""Dev-startup schema creation that stays compatible with `make migrate`.

The services create their tables at startup with ``Base.metadata.create_all``
(a dev convenience; production runs Alembic). ``create_all`` never writes an
``alembic_version`` row, so a long-lived create_all-born volume cannot adopt
migrations: ``make migrate`` runs each chain from base against objects that
already exist and fails, and ``create_all`` itself cannot ALTER an existing
table to add a newly merged column (issue #278).

``create_all_and_stamp`` fixes the forward path without hiding the broken one:

- Fresh schema (no service tables yet): create the tables, then stamp the
  Alembic head so a later ``make migrate`` applies only new increments.
- Legacy create_all-born schema (tables exist, no stamp): do NOT stamp, because
  stamping head would falsely claim every merged migration is applied and mask
  genuinely missing columns. Log a loud, actionable warning instead.
- Already migration-managed (an ``alembic_version`` row exists): leave it alone.

The decision keys off the state observed BEFORE ``create_all`` runs, so a table
that ``create_all`` is about to add does not flip a legacy volume to "fresh".
"""

import logging
from enum import Enum
from pathlib import Path

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_ALEMBIC_VERSION_TABLE = "alembic_version"


class SchemaInitResult(str, Enum):
    STAMPED_FRESH = "stamped_fresh"
    UNSTAMPED_LEGACY = "unstamped_legacy"
    ALREADY_MANAGED = "already_managed"


def decide_schema_action(*, had_tables: bool, had_stamp: bool) -> SchemaInitResult:
    """Classify a schema from its pre-create_all state.

    An existing stamp always wins: a stamped schema is migration-managed
    regardless of table state. Otherwise, no tables means a fresh schema safe to
    stamp, and tables without a stamp is the broken create_all-born volume.
    """
    if had_stamp:
        return SchemaInitResult.ALREADY_MANAGED
    if not had_tables:
        return SchemaInitResult.STAMPED_FRESH
    return SchemaInitResult.UNSTAMPED_LEGACY


def _qualified(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _inspect_state(
    connection: Connection, metadata: MetaData, schema: str | None
) -> tuple[bool, bool]:
    """Return (had_tables, had_stamp) for the schema before create_all runs."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names(schema=schema))
    model_tables = {table.name for table in metadata.tables.values()}
    had_tables = bool(existing & model_tables)

    had_stamp = False
    if _ALEMBIC_VERSION_TABLE in existing:
        row = connection.execute(
            text(f"SELECT version_num FROM {_qualified(schema, _ALEMBIC_VERSION_TABLE)} LIMIT 1")
        ).first()
        had_stamp = row is not None
    return had_tables, had_stamp


def _stamp_head(connection: Connection, schema: str | None, script_location: str) -> str | None:
    """Stamp the Alembic head into the schema, matching `alembic upgrade` layout.

    Returns the stamped revision, or None when the chain has no revisions.
    """
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", script_location)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        return None

    context = MigrationContext.configure(
        connection=connection,
        opts={"version_table_schema": schema},
    )
    context.stamp(script, head)
    return head


async def create_all_and_stamp(
    engine: AsyncEngine,
    metadata: MetaData,
    *,
    schema: str | None,
    script_location: str | Path,
    log: logging.Logger | None = None,
) -> SchemaInitResult:
    """Run create_all, then stamp the Alembic head only on a genuinely fresh schema.

    ``schema`` is the service's ``db_schema`` (None for the schemaless SQLite
    test databases). ``script_location`` points at the service's ``migrations``
    directory. Idempotent: safe on every startup.
    """
    log = log or logger
    script_location = str(script_location)
    # An empty db_schema (the schemaless SQLite test databases) means "default
    # schema"; SQLAlchemy and Alembic express that as None, not "".
    schema = schema or None

    async with engine.begin() as conn:
        had_tables, had_stamp = await conn.run_sync(_inspect_state, metadata, schema)
        await conn.run_sync(metadata.create_all)
        action = decide_schema_action(had_tables=had_tables, had_stamp=had_stamp)

        if action is SchemaInitResult.STAMPED_FRESH:
            head = await conn.run_sync(_stamp_head, schema, script_location)
            if head is None:
                log.warning(
                    "Fresh schema '%s' created but no Alembic revisions were found at %s; "
                    "left unstamped.",
                    schema,
                    script_location,
                )
            else:
                log.info(
                    "Fresh schema '%s' created; stamped Alembic head %s so a later "
                    "`make migrate` applies future increments.",
                    schema,
                    head,
                )
        elif action is SchemaInitResult.UNSTAMPED_LEGACY:
            log.warning(
                "Schema '%s' has tables but no Alembic version stamp: a legacy "
                "create_all-born volume. `make migrate` will fail (objects already "
                "exist) and newly merged migration columns are missing, so affected "
                "routes may return 500. Recreate the Postgres volume "
                "(`docker compose down -v` then bring the stack back up) to fix. "
                "See docs/TROUBLESHOOTING.md.",
                schema,
            )

    return action
