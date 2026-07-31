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
  genuinely missing columns. Log a loud, actionable warning instead. create_all
  still runs here (the historical limp-along behavior for a volume whose only
  real fix is recreation).
- Already migration-managed (an ``alembic_version`` row exists): do NOT run
  ``create_all`` at all; a stamped schema evolves only through Alembic
  (issue #419). Running create_all here used to silently pre-create any table a
  newly deployed model declares without advancing ``alembic_version``, so the
  table's own migration later crashed with DuplicateTableError on an
  upgraded-in-place stack. Skipping create_all kills that hazard for every
  new-table migration, past and future, without per-migration ``has_table``
  guards. The cost is that an upgraded-in-place stack does not see new tables
  until ``make migrate-<svc>`` runs; startup logs an actionable warning when it
  detects the stamp behind the chain head.

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
) -> tuple[bool, str | None, set[str]]:
    """Return (had_tables, stamp, missing_model_tables) before create_all runs.

    ``stamp`` is the recorded ``alembic_version`` revision, or None when the
    schema carries no stamp. ``missing_model_tables`` are model tables absent
    from the database, used only for advisory drift logging on the managed path.
    """
    inspector = inspect(connection)
    existing = set(inspector.get_table_names(schema=schema))
    model_tables = {table.name for table in metadata.tables.values()}
    had_tables = bool(existing & model_tables)
    missing_model_tables = model_tables - existing

    stamp: str | None = None
    if _ALEMBIC_VERSION_TABLE in existing:
        row = connection.execute(
            text(f"SELECT version_num FROM {_qualified(schema, _ALEMBIC_VERSION_TABLE)} LIMIT 1")
        ).first()
        stamp = row[0] if row is not None else None
    return had_tables, stamp, missing_model_tables


def _script_directory(script_location: str):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", script_location)
    return ScriptDirectory.from_config(config)


def _stamp_head(connection: Connection, schema: str | None, script_location: str) -> str | None:
    """Stamp the Alembic head into the schema, matching `alembic upgrade` layout.

    Returns the stamped revision, or None when the chain has no revisions.
    """
    from alembic.migration import MigrationContext

    script = _script_directory(script_location)
    head = script.get_current_head()
    if head is None:
        return None

    context = MigrationContext.configure(
        connection=connection,
        opts={"version_table_schema": schema},
    )
    context.stamp(script, head)
    return head


def _log_managed_schema_drift(
    log: logging.Logger,
    *,
    schema: str | None,
    script_location: str,
    stamp: str | None,
    had_tables: bool,
    missing_model_tables: set[str],
) -> None:
    """Advisory-only drift check for a migration-managed schema.

    Never raises: this path did not touch the Alembic script directory before
    issue #419, so a broken or missing script directory must not become a new
    boot failure mode.
    """
    if missing_model_tables and not had_tables:
        # A stamp with ZERO model tables: a manual `alembic stamp` on an empty
        # schema, or a partial restore that kept alembic_version. `make migrate`
        # cannot fix this: the stamp claims the missing tables' migrations
        # already ran, so upgrade replays nothing.
        log.warning(
            "Schema '%s' carries an Alembic stamp (%s) but none of the service's "
            "model tables exist: the stamp does not match the actual schema "
            "contents (a manual stamp on an empty schema, or a partial restore "
            "that kept alembic_version). `make migrate` will NOT recreate tables "
            "the stamp claims exist. Either restore the schema contents or drop "
            "the alembic_version row so the next boot can bootstrap it fresh.",
            schema,
            stamp,
        )
        return

    try:
        script = _script_directory(script_location)
        head = script.get_current_head()
    except Exception as exc:  # advisory only; never block boot
        log.warning(
            "Schema '%s' is migration-managed; could not read the Alembic chain at %s "
            "to check for pending migrations: %s",
            schema,
            script_location,
            exc,
        )
        return

    if head is not None and stamp != head:
        try:
            script.get_revision(stamp)
            stamp_in_chain = True
        except Exception:
            stamp_in_chain = False
        if stamp_in_chain:
            # The chain is linear, so a resolvable non-head stamp is behind head.
            log.warning(
                "Schema '%s' is migration-managed and stamped at %s, but this build's "
                "Alembic head is %s. Startup no longer pre-creates model tables on a "
                "managed schema (issue #419), so tables from unapplied migrations are "
                "absent until `make migrate` (or `make migrate-<svc>`) runs.",
                schema,
                stamp,
                head,
            )
        else:
            log.warning(
                "Schema '%s' is stamped at %s, which is not in this build's migration "
                "chain (head %s): the schema was migrated by a newer build than this "
                "image (a rollback). Startup makes no schema changes; behavior is "
                "read-compatible best-effort until the image and schema versions "
                "match again.",
                schema,
                stamp,
                head,
            )
    elif missing_model_tables:
        log.warning(
            "Schema '%s' is stamped at the Alembic head %s, but model tables %s do "
            "not exist and no migration is pending: a new-table migration appears "
            "to be missing from the chain. Startup does not create tables on a "
            "managed schema (issue #419); add the migration and run `make migrate`.",
            schema,
            stamp,
            sorted(missing_model_tables),
        )


async def create_all_and_stamp(
    engine: AsyncEngine,
    metadata: MetaData,
    *,
    schema: str | None,
    script_location: str | Path,
    log: logging.Logger | None = None,
) -> SchemaInitResult:
    """Create tables only on a schema Alembic does not manage yet; stamp when fresh.

    ``schema`` is the service's ``db_schema`` (None for the schemaless SQLite
    test databases). ``script_location`` points at the service's ``migrations``
    directory. Idempotent: safe on every startup.

    A schema with an ``alembic_version`` stamp is never touched: no create_all,
    no stamp (issue #419). Pending migrations there are applied by
    ``make migrate``, never implicitly at boot, so a bare ``op.create_table``
    migration can never collide with a table create_all pre-created.
    """
    log = log or logger
    script_location = str(script_location)
    # An empty db_schema (the schemaless SQLite test databases) means "default
    # schema"; SQLAlchemy and Alembic express that as None, not "".
    schema = schema or None

    async with engine.begin() as conn:
        had_tables, stamp, missing_model_tables = await conn.run_sync(
            _inspect_state, metadata, schema
        )
        action = decide_schema_action(had_tables=had_tables, had_stamp=stamp is not None)

        if action is SchemaInitResult.ALREADY_MANAGED:
            _log_managed_schema_drift(
                log,
                schema=schema,
                script_location=script_location,
                stamp=stamp,
                had_tables=had_tables,
                missing_model_tables=missing_model_tables,
            )
            return action

        await conn.run_sync(metadata.create_all)

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
